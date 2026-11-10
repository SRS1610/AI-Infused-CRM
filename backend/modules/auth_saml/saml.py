"""SAML 2.0 Web Browser SSO, service-provider side.

Builds AuthnRequests for the HTTP-Redirect binding and the SP metadata document, and validates
responses received through the HTTP-POST binding. Nothing here touches the database: the caller
passes an :class:`SpConfig` and gets back a :class:`ValidatedAssertion` or a :class:`SamlError`.

Validation rules (SAML 2.0 core §2-3, profiles §4.1, and the usual hardening):

* the document is parsed with a hardened parser (no DTD, no entities, no network, no huge trees);
* signatures are checked with ``signxml`` against the configured IdP certificates only — a
  certificate carried in the message is never trusted;
* only the element the verifier returns as signed is read; a document with more than one assertion
  anywhere, or an encrypted assertion, is refused (signature wrapping);
* status, issuer, audience, recipient/destination, InResponseTo and validity windows (with clock
  skew) are checked on the signed data.
"""

from __future__ import annotations

import base64
import binascii
import datetime as dt
import re
import secrets
import urllib.parse
import zlib
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec, padding, rsa
from cryptography.x509.oid import NameOID
from lxml import etree
from signxml.algorithms import DigestAlgorithm, SignatureMethod
from signxml.exceptions import InvalidCertificate, InvalidDigest, InvalidInput, InvalidSignature
from signxml.verifier import SignatureConfiguration, XMLVerifier

from tessera.i18n import _

NS_SAMLP = "urn:oasis:names:tc:SAML:2.0:protocol"
NS_SAML = "urn:oasis:names:tc:SAML:2.0:assertion"
NS_MD = "urn:oasis:names:tc:SAML:2.0:metadata"
NS_DS = "http://www.w3.org/2000/09/xmldsig#"
NS = {"samlp": NS_SAMLP, "saml": NS_SAML, "md": NS_MD, "ds": NS_DS}

BINDING_POST = "urn:oasis:names:tc:SAML:2.0:bindings:HTTP-POST"
BINDING_REDIRECT = "urn:oasis:names:tc:SAML:2.0:bindings:HTTP-Redirect"
STATUS_SUCCESS = "urn:oasis:names:tc:SAML:2.0:status:Success"
CM_BEARER = "urn:oasis:names:tc:SAML:2.0:cm:bearer"
NAMEID_FORMATS = {
    "unspecified": "urn:oasis:names:tc:SAML:1.1:nameid-format:unspecified",
    "email": "urn:oasis:names:tc:SAML:1.1:nameid-format:emailAddress",
    "persistent": "urn:oasis:names:tc:SAML:2.0:nameid-format:persistent",
}
NAMEID_TRANSIENT = "urn:oasis:names:tc:SAML:2.0:nameid-format:transient"

# SHA-1, HMAC and DSA are refused: the IdP signs with its certificate's key using SHA-2
SIGNATURE_METHODS = frozenset(
    {
        SignatureMethod.RSA_SHA256,
        SignatureMethod.RSA_SHA384,
        SignatureMethod.RSA_SHA512,
        SignatureMethod.ECDSA_SHA256,
        SignatureMethod.ECDSA_SHA384,
        SignatureMethod.ECDSA_SHA512,
        SignatureMethod.SHA256_RSA_MGF1,
        SignatureMethod.SHA384_RSA_MGF1,
        SignatureMethod.SHA512_RSA_MGF1,
    }
)
DIGEST_ALGORITHMS = frozenset({DigestAlgorithm.SHA256, DigestAlgorithm.SHA384, DigestAlgorithm.SHA512})
REDIRECT_SIG_ALGS = {
    "rsa": "http://www.w3.org/2001/04/xmldsig-more#rsa-sha256",
    "ec": "http://www.w3.org/2001/04/xmldsig-more#ecdsa-sha256",
}

MAX_RESPONSE_BYTES = 512 * 1024  # decoded SAMLResponse; real responses are a few kilobytes
_DTD_MARKERS = re.compile(rb"<!\s*(DOCTYPE|ENTITY)", re.IGNORECASE)


def _q(ns: str, tag: str) -> str:
    return f"{{{ns}}}{tag}"


T_RESPONSE = _q(NS_SAMLP, "Response")
T_AUTHN_REQUEST = _q(NS_SAMLP, "AuthnRequest")
T_STATUS = _q(NS_SAMLP, "Status")
T_STATUS_CODE = _q(NS_SAMLP, "StatusCode")
T_ASSERTION = _q(NS_SAML, "Assertion")
T_ENCRYPTED_ASSERTION = _q(NS_SAML, "EncryptedAssertion")
T_ISSUER = _q(NS_SAML, "Issuer")
T_SUBJECT = _q(NS_SAML, "Subject")
T_NAMEID = _q(NS_SAML, "NameID")
T_ENCRYPTED_ID = _q(NS_SAML, "EncryptedID")
T_SUBJECT_CONFIRMATION = _q(NS_SAML, "SubjectConfirmation")
T_SUBJECT_CONFIRMATION_DATA = _q(NS_SAML, "SubjectConfirmationData")
T_CONDITIONS = _q(NS_SAML, "Conditions")
T_AUDIENCE_RESTRICTION = _q(NS_SAML, "AudienceRestriction")
T_AUDIENCE = _q(NS_SAML, "Audience")
T_AUTHN_STATEMENT = _q(NS_SAML, "AuthnStatement")
T_ATTRIBUTE_STATEMENT = _q(NS_SAML, "AttributeStatement")
T_ATTRIBUTE = _q(NS_SAML, "Attribute")
T_ATTRIBUTE_VALUE = _q(NS_SAML, "AttributeValue")
T_ENCRYPTED_ATTRIBUTE = _q(NS_SAML, "EncryptedAttribute")
T_SIGNATURE = _q(NS_DS, "Signature")


class SamlError(Exception):
    """A refused SAML message. ``code`` is a stable reason for logs; ``message`` is shown to the user."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


@dataclass(frozen=True)
class SpConfig:
    """What the validator needs to know about one identity provider and this service provider."""

    idp_entity_id: str
    idp_certificates: Sequence[x509.Certificate]
    sp_entity_id: str
    acs_url: str
    clock_skew: int = 120
    want_assertions_signed: bool = True


@dataclass
class ValidatedAssertion:
    """Data read from the signed assertion only."""

    assertion_id: str
    issuer: str
    name_id: str
    name_id_format: str | None
    in_response_to: str
    valid_until: dt.datetime  # until when the assertion id must stay in the replay cache
    attributes: dict[str, list[str]] = field(default_factory=dict)
    session_index: str | None = None

    def attribute(self, name: str) -> str | None:
        for value in self.attributes.get(name, []):
            if value.strip():
                return value.strip()
        return None


# -- certificates and keys --------------------------------------------------------------------------------


def load_certificates(pem: str | None) -> list[x509.Certificate]:
    """Every certificate in a PEM bundle (several are allowed during an IdP key rotation)."""
    text = (pem or "").strip()
    if not text:
        return []
    if "-----BEGIN" not in text:
        # a bare base64 body, as copied from IdP metadata
        body = "".join(text.split())
        text = f"-----BEGIN CERTIFICATE-----\n{body}\n-----END CERTIFICATE-----"
    try:
        return list(x509.load_pem_x509_certificates(text.encode("ascii")))
    except (ValueError, UnicodeEncodeError) as exc:
        raise SamlError("bad_certificate", _("The certificate is not a valid PEM X.509 certificate")) from exc


def load_private_key(pem: str) -> rsa.RSAPrivateKey | ec.EllipticCurvePrivateKey:
    try:
        key = serialization.load_pem_private_key(pem.encode("ascii"), password=None)
    except (ValueError, TypeError, UnicodeEncodeError) as exc:
        raise SamlError("bad_key", _("The private key is not an unencrypted PEM key")) from exc
    if not isinstance(key, rsa.RSAPrivateKey | ec.EllipticCurvePrivateKey):
        raise SamlError("bad_key", _("Only RSA and EC private keys are supported"))
    return key


def key_matches_certificate(key: rsa.RSAPrivateKey | ec.EllipticCurvePrivateKey, cert: x509.Certificate) -> bool:
    fmt = serialization.PublicFormat.SubjectPublicKeyInfo
    enc = serialization.Encoding.DER
    return bool(key.public_key().public_bytes(enc, fmt) == cert.public_key().public_bytes(enc, fmt))


def generate_sp_keypair(common_name: str, *, days: int = 3650) -> tuple[str, str]:
    """A new RSA-3072 key and a self-signed certificate for signing AuthnRequests (PEM key, PEM cert)."""
    key = rsa.generate_private_key(public_exponent=65537, key_size=3072)
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, common_name[:64] or "Tessera SP")])
    now = dt.datetime.now(dt.UTC)
    cert = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - dt.timedelta(minutes=5))
        .not_valid_after(now + dt.timedelta(days=days))
        .sign(key, hashes.SHA256())
    )
    key_pem = key.private_bytes(
        serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()
    ).decode("ascii")
    return key_pem, cert.public_bytes(serialization.Encoding.PEM).decode("ascii")


def certificate_body(cert: x509.Certificate) -> str:
    """Base64 DER, as written in ``ds:X509Certificate``."""
    return base64.b64encode(cert.public_bytes(serialization.Encoding.DER)).decode("ascii")


# -- time -------------------------------------------------------------------------------------------------


def _format_instant(value: dt.datetime) -> str:
    return value.astimezone(dt.UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _parse_instant(value: str | None, what: str) -> dt.datetime | None:
    if value is None:
        return None
    text = value.strip()
    try:
        parsed = dt.datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError as exc:
        raise SamlError("bad_time", _("The identity provider sent an invalid time ({what})", what=what)) from exc
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=dt.UTC)  # SAML times are UTC


# -- outgoing messages ------------------------------------------------------------------------------------


def new_message_id() -> str:
    """An xs:ID (NCName: must not start with a digit) with 160 bits of randomness."""
    return "_" + secrets.token_hex(20)


def build_authn_request(
    *,
    request_id: str,
    sp_entity_id: str,
    acs_url: str,
    destination: str,
    name_id_format: str | None,
    now: dt.datetime,
) -> bytes:
    nsmap = {"samlp": NS_SAMLP, "saml": NS_SAML}
    root = etree.Element(
        T_AUTHN_REQUEST,
        nsmap=nsmap,
        ID=request_id,
        Version="2.0",
        IssueInstant=_format_instant(now),
        Destination=destination,
        ProtocolBinding=BINDING_POST,
        AssertionConsumerServiceURL=acs_url,
    )
    issuer = etree.SubElement(root, T_ISSUER)
    issuer.text = sp_entity_id
    policy = etree.SubElement(root, _q(NS_SAMLP, "NameIDPolicy"), AllowCreate="true")
    if name_id_format:
        policy.set("Format", name_id_format)
    return bytes(etree.tostring(root, xml_declaration=False, encoding="utf-8"))


def _quote(value: str) -> str:
    return urllib.parse.quote(value, safe="")


def redirect_url(
    idp_sso_url: str,
    authn_request: bytes,
    relay_state: str,
    *,
    signing_key: rsa.RSAPrivateKey | ec.EllipticCurvePrivateKey | None = None,
) -> str:
    """HTTP-Redirect binding (bindings §3.4): raw DEFLATE, base64, URL-encoded; optionally signed in the
    query string (the signature covers ``SAMLRequest=…&RelayState=…&SigAlg=…`` exactly as sent)."""
    compressor = zlib.compressobj(9, zlib.DEFLATED, -15)
    deflated = compressor.compress(authn_request) + compressor.flush()
    query = "SAMLRequest=" + _quote(base64.b64encode(deflated).decode("ascii"))
    query += "&RelayState=" + _quote(relay_state)
    if signing_key is not None:
        if isinstance(signing_key, rsa.RSAPrivateKey):
            sig_alg = REDIRECT_SIG_ALGS["rsa"]
            query += "&SigAlg=" + _quote(sig_alg)
            signature = signing_key.sign(query.encode("ascii"), padding.PKCS1v15(), hashes.SHA256())
        else:
            sig_alg = REDIRECT_SIG_ALGS["ec"]
            query += "&SigAlg=" + _quote(sig_alg)
            signature = signing_key.sign(query.encode("ascii"), ec.ECDSA(hashes.SHA256()))
        query += "&Signature=" + _quote(base64.b64encode(signature).decode("ascii"))
    separator = "&" if urllib.parse.urlsplit(idp_sso_url).query else "?"
    return f"{idp_sso_url}{separator}{query}"


def sp_metadata(
    *,
    sp_entity_id: str,
    acs_url: str,
    name_id_format: str | None,
    want_assertions_signed: bool,
    signing_certificate: x509.Certificate | None,
) -> bytes:
    nsmap = {"md": NS_MD, "ds": NS_DS}
    root = etree.Element(_q(NS_MD, "EntityDescriptor"), nsmap=nsmap, entityID=sp_entity_id)
    sp = etree.SubElement(
        root,
        _q(NS_MD, "SPSSODescriptor"),
        AuthnRequestsSigned="true" if signing_certificate is not None else "false",
        WantAssertionsSigned="true" if want_assertions_signed else "false",
        protocolSupportEnumeration=NS_SAMLP,
    )
    if signing_certificate is not None:
        descriptor = etree.SubElement(sp, _q(NS_MD, "KeyDescriptor"), use="signing")
        key_info = etree.SubElement(descriptor, _q(NS_DS, "KeyInfo"))
        x509_data = etree.SubElement(key_info, _q(NS_DS, "X509Data"))
        etree.SubElement(x509_data, _q(NS_DS, "X509Certificate")).text = certificate_body(signing_certificate)
    if name_id_format:
        etree.SubElement(sp, _q(NS_MD, "NameIDFormat")).text = name_id_format
    etree.SubElement(
        sp, _q(NS_MD, "AssertionConsumerService"), Binding=BINDING_POST, Location=acs_url, index="0", isDefault="true"
    )
    return bytes(etree.tostring(root, xml_declaration=True, encoding="utf-8", pretty_print=True))


# -- incoming responses -----------------------------------------------------------------------------------


def hardened_parser() -> etree.XMLParser:
    """No DTD loading or validation, no entity expansion, no network access, bounded document size."""
    return etree.XMLParser(
        resolve_entities=False,
        no_network=True,
        load_dtd=False,
        dtd_validation=False,
        huge_tree=False,
        collect_ids=False,
    )


def parse_xml(data: bytes) -> Any:
    """Parse untrusted XML; refuses DTDs and entity declarations or references."""
    if _DTD_MARKERS.search(data):
        raise SamlError("dtd", _("The SAML response contains a document type declaration; it was refused"))
    try:
        root = etree.fromstring(data, parser=hardened_parser())
    except etree.XMLSyntaxError as exc:
        raise SamlError("malformed", _("The SAML response is not well-formed XML")) from exc
    docinfo = root.getroottree().docinfo
    if docinfo.internalDTD is not None or docinfo.externalDTD is not None or docinfo.doctype:
        raise SamlError("dtd", _("The SAML response contains a document type declaration; it was refused"))
    for _entity in root.iter(etree.Entity):
        raise SamlError("dtd", _("The SAML response contains a document type declaration; it was refused"))
    return root


def decode_post_response(saml_response: str) -> bytes:
    """The base64 ``SAMLResponse`` form field of the HTTP-POST binding."""
    compact = "".join((saml_response or "").split())
    if not compact:
        raise SamlError("missing", _("The identity provider sent no SAML response"))
    if len(compact) > MAX_RESPONSE_BYTES * 4 // 3 + 4:
        raise SamlError("too_large", _("The SAML response is too large"))
    try:
        return base64.b64decode(compact, validate=True)
    except (binascii.Error, ValueError) as exc:
        raise SamlError("malformed", _("The SAML response is not valid base64")) from exc


def _verify(data: bytes, certificates: Sequence[x509.Certificate], expected_tag: str, expected_id: str) -> Any:
    """Verify the enveloped signature that is a direct child of the document element of ``data`` against each
    configured certificate in turn; return the element the verifier reports as signed (and only that)."""
    if not certificates:
        raise SamlError("no_certificate", _("No identity provider certificate is configured"))
    config = SignatureConfiguration(
        require_x509=True,
        location="./",
        expect_references=1,
        signature_methods=SIGNATURE_METHODS,
        digest_algorithms=DIGEST_ALGORITHMS,
    )
    for cert in certificates:
        try:
            result = XMLVerifier().verify(
                data, x509_cert=cert, id_attribute="ID", parser=hardened_parser(), expect_config=config
            )
        except (InvalidSignature, InvalidDigest, InvalidCertificate, InvalidInput, etree.XMLSyntaxError, ValueError):
            continue
        except (etree.DocumentInvalid, TypeError, AttributeError, KeyError, IndexError):  # malformed: fail closed
            continue
        signed = getattr(result, "signed_xml", None)
        if signed is None or signed.tag != expected_tag or signed.get("ID") != expected_id:
            # the signature is valid but covers something else than the element it sits in
            raise SamlError("signature_reference", _("The signature does not cover the signed element"))
        return signed
    raise SamlError("signature_invalid", _("The signature could not be verified with the configured certificates"))


def _children(element: Any, tag: str) -> list[Any]:
    return [child for child in element if child.tag == tag]


def _one_child(element: Any, tag: str, code: str, message: str) -> Any:
    found = _children(element, tag)
    if len(found) != 1:
        raise SamlError(code, message)
    return found[0]


def _text(element: Any | None) -> str:
    return "".join(element.itertext()).strip() if element is not None else ""


def validate_response(
    xml: bytes,
    config: SpConfig,
    *,
    expected_request_ids: Sequence[str] | None = None,
    now: dt.datetime | None = None,
) -> ValidatedAssertion:
    """Validate a decoded ``SAMLResponse`` and return what its signed assertion says.

    ``expected_request_ids``: when given, InResponseTo must be one of them (the caller then checks the
    request is still pending and bound to this browser). Raises :class:`SamlError`."""
    now = now or dt.datetime.now(dt.UTC)
    skew = dt.timedelta(seconds=max(0, int(config.clock_skew)))
    if len(xml) > MAX_RESPONSE_BYTES:
        raise SamlError("too_large", _("The SAML response is too large"))
    root = parse_xml(xml)
    if root.tag != T_RESPONSE:
        raise SamlError("not_a_response", _("The message is not a SAML response"))
    if root.get("Version") != "2.0":
        raise SamlError("version", _("Only SAML 2.0 responses are accepted"))
    if any(True for _el in root.iter(T_ENCRYPTED_ASSERTION, T_ENCRYPTED_ID, T_ENCRYPTED_ATTRIBUTE)):
        raise SamlError(
            "encrypted",
            _(
                "Encrypted assertions are not supported; turn off assertion encryption for this application at "
                "the identity provider"
            ),
        )
    # signature wrapping defence: exactly one assertion in the whole document, as a direct child of the response
    if sum(1 for _el in root.iter(T_ASSERTION)) != 1 or len(_children(root, T_ASSERTION)) != 1:
        raise SamlError("assertion_count", _("The SAML response must contain exactly one assertion"))

    response_id = root.get("ID") or ""
    signed_response = None
    if _children(root, T_SIGNATURE):
        signed_response = _verify(xml, config.idp_certificates, T_RESPONSE, response_id)
    response = signed_response if signed_response is not None else root

    status = response.find("samlp:Status/samlp:StatusCode", NS)
    if status is None or status.get("Value") != STATUS_SUCCESS:
        raise SamlError("status", _("The identity provider did not complete the login"))
    issuer = _children(response, T_ISSUER)
    if issuer and _text(issuer[0]) != config.idp_entity_id:
        raise SamlError("issuer", _("The response comes from an unexpected identity provider"))
    destination = response.get("Destination")
    if destination is not None and destination != config.acs_url:
        raise SamlError("destination", _("The response was sent to another address"))
    if signed_response is not None and destination is None:
        raise SamlError("destination", _("The response was sent to another address"))

    original = _children(root, T_ASSERTION)[0]
    assertion_id = original.get("ID") or ""
    if not assertion_id:
        raise SamlError("assertion_id", _("The assertion has no identifier"))
    if _children(original, T_SIGNATURE):
        # verified on its own, from the received document (its namespace context intact); the verifier hands
        # back the canonical signed assertion, which is all that is read from here on
        assertion = _verify(etree.tostring(original), config.idp_certificates, T_ASSERTION, assertion_id)
    elif signed_response is None:
        raise SamlError("unsigned", _("The SAML response is not signed"))
    elif config.want_assertions_signed:
        raise SamlError("assertion_unsigned", _("The assertion is not signed"))
    else:
        # covered by the response signature: read it from the signed response returned by the verifier
        assertion = _one_child(
            signed_response, T_ASSERTION, "assertion_count", _("The SAML response must contain exactly one assertion")
        )
    if assertion.get("ID") != assertion_id or any(True for el in assertion.iter(T_ASSERTION) if el is not assertion):
        raise SamlError("assertion_count", _("The SAML response must contain exactly one assertion"))

    return _read_assertion(assertion, config, now, skew, response.get("InResponseTo"), expected_request_ids)


def _read_assertion(
    assertion: Any,
    config: SpConfig,
    now: dt.datetime,
    skew: dt.timedelta,
    response_in_response_to: str | None,
    expected_request_ids: Sequence[str] | None,
) -> ValidatedAssertion:
    if assertion.get("Version") != "2.0":
        raise SamlError("version", _("Only SAML 2.0 responses are accepted"))
    issuer = _one_child(assertion, T_ISSUER, "issuer", _("The response comes from an unexpected identity provider"))
    if _text(issuer) != config.idp_entity_id:
        raise SamlError("issuer", _("The response comes from an unexpected identity provider"))

    # conditions: validity window and audience
    conditions = _one_child(assertion, T_CONDITIONS, "conditions", _("The assertion has no conditions"))
    not_before = _parse_instant(conditions.get("NotBefore"), "NotBefore")
    not_on_or_after = _parse_instant(conditions.get("NotOnOrAfter"), "NotOnOrAfter")
    if not_before is not None and now + skew < not_before:
        raise SamlError("not_yet_valid", _("The assertion is not valid yet; check the server clocks"))
    if not_on_or_after is not None and now - skew >= not_on_or_after:
        raise SamlError("expired", _("The assertion has expired; sign in again"))
    restrictions = _children(conditions, T_AUDIENCE_RESTRICTION)
    if not restrictions:
        raise SamlError("audience", _("The assertion is meant for another application"))
    for restriction in restrictions:  # the SP must be named in every restriction (core §2.5.1.4)
        if config.sp_entity_id not in {_text(a) for a in _children(restriction, T_AUDIENCE)}:
            raise SamlError("audience", _("The assertion is meant for another application"))

    # subject and bearer confirmation
    subject = _one_child(assertion, T_SUBJECT, "subject", _("The assertion has no subject"))
    name_id_el = _one_child(subject, T_NAMEID, "subject", _("The assertion has no subject"))
    name_id = _text(name_id_el)
    name_id_format = name_id_el.get("Format")
    if not name_id:
        raise SamlError("subject", _("The assertion has no subject"))
    if name_id_format == NAMEID_TRANSIENT:
        raise SamlError(
            "transient_nameid",
            _("The identity provider sent a transient NameID; configure a persistent or e-mail NameID"),
        )
    confirmation_until: dt.datetime | None = None
    in_response_to: str | None = None
    for confirmation in _children(subject, T_SUBJECT_CONFIRMATION):
        if confirmation.get("Method") != CM_BEARER:
            continue
        data = _children(confirmation, T_SUBJECT_CONFIRMATION_DATA)
        if len(data) != 1:
            continue
        scd = data[0]
        until = _parse_instant(scd.get("NotOnOrAfter"), "SubjectConfirmationData NotOnOrAfter")
        since = _parse_instant(scd.get("NotBefore"), "SubjectConfirmationData NotBefore")
        if scd.get("Recipient") != config.acs_url:
            continue
        if until is None or now - skew >= until or (since is not None and now + skew < since):
            continue
        candidate = scd.get("InResponseTo")
        if expected_request_ids is not None and candidate not in expected_request_ids:
            continue
        confirmation_until, in_response_to = until, candidate
        break
    if confirmation_until is None:
        raise _confirmation_error(subject, config, now, skew, expected_request_ids)
    if not in_response_to:
        raise SamlError(
            "unsolicited", _("Logins started at the identity provider are not accepted; start from the login page")
        )
    if response_in_response_to is not None and response_in_response_to != in_response_to:
        raise SamlError("in_response_to", _("The response does not answer a login started here"))

    authn = _children(assertion, T_AUTHN_STATEMENT)
    if not authn:
        raise SamlError("authn_statement", _("The assertion does not say how the user authenticated"))
    session_until = _parse_instant(authn[0].get("SessionNotOnOrAfter"), "SessionNotOnOrAfter")
    if session_until is not None and now - skew >= session_until:
        raise SamlError("expired", _("The assertion has expired; sign in again"))

    attributes: dict[str, list[str]] = {}
    for statement in _children(assertion, T_ATTRIBUTE_STATEMENT):
        for attribute in _children(statement, T_ATTRIBUTE):
            name = attribute.get("Name")
            if not name:
                continue
            values = [_text(v) for v in _children(attribute, T_ATTRIBUTE_VALUE)]
            attributes.setdefault(name, []).extend(values)
            friendly = attribute.get("FriendlyName")
            if friendly and friendly != name:
                attributes.setdefault(friendly, []).extend(values)

    valid_until = max(d for d in (confirmation_until, not_on_or_after) if d is not None) + skew
    return ValidatedAssertion(
        assertion_id=assertion.get("ID"),
        issuer=config.idp_entity_id,
        name_id=name_id,
        name_id_format=name_id_format,
        in_response_to=in_response_to,
        valid_until=valid_until,
        attributes=attributes,
        session_index=authn[0].get("SessionIndex"),
    )


def _confirmation_error(
    subject: Any, config: SpConfig, now: dt.datetime, skew: dt.timedelta, expected: Sequence[str] | None
) -> SamlError:
    """Name the first reason no bearer confirmation was acceptable (for the user and the logs)."""
    for confirmation in _children(subject, T_SUBJECT_CONFIRMATION):
        if confirmation.get("Method") != CM_BEARER:
            continue
        for scd in _children(confirmation, T_SUBJECT_CONFIRMATION_DATA):
            if scd.get("Recipient") != config.acs_url:
                return SamlError("recipient", _("The response was sent to another address"))
            until = _parse_instant(scd.get("NotOnOrAfter"), "SubjectConfirmationData NotOnOrAfter")
            since = _parse_instant(scd.get("NotBefore"), "SubjectConfirmationData NotBefore")
            if until is None or now - skew >= until:
                return SamlError("expired", _("The assertion has expired; sign in again"))
            if since is not None and now + skew < since:
                return SamlError("not_yet_valid", _("The assertion is not valid yet; check the server clocks"))
            if not scd.get("InResponseTo"):
                return SamlError(
                    "unsolicited",
                    _("Logins started at the identity provider are not accepted; start from the login page"),
                )
            if expected is not None and scd.get("InResponseTo") not in expected:
                return SamlError("in_response_to", _("The response does not answer a login started here"))
    return SamlError("confirmation", _("The assertion has no usable bearer confirmation"))
