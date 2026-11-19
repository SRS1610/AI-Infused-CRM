"""A throw-away identity provider for tests: its own key and self-signed certificate, and responses signed
with signxml the way real identity providers sign them (enveloped, exclusive canonicalisation, SHA-256)."""

from __future__ import annotations

import base64
import datetime as dt
import uuid
from typing import Any

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID
from lxml import etree
from signxml import CanonicalizationMethod, DigestAlgorithm, SignatureMethod, XMLSigner

from modules.auth_saml.saml import CM_BEARER, NAMEID_FORMATS, NS_DS, NS_SAML, NS_SAMLP, STATUS_SUCCESS

ENTITY_ID = "https://idp.example.test/saml/metadata"
SSO_URL = "https://idp.example.test/saml/sso"


def new_identity(common_name: str = "Test IdP") -> tuple[rsa.RSAPrivateKey, x509.Certificate, str]:
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, common_name)])
    now = dt.datetime.now(dt.UTC)
    cert = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - dt.timedelta(days=1))
        .not_valid_after(now + dt.timedelta(days=365))
        .sign(key, hashes.SHA256())
    )
    return key, cert, cert.public_bytes(serialization.Encoding.PEM).decode("ascii")


def _iso(value: dt.datetime) -> str:
    return value.astimezone(dt.UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _el(parent: Any, ns: str, tag: str, text: str | None = None, **attrs: str) -> Any:
    element = etree.SubElement(parent, f"{{{ns}}}{tag}", **{k: v for k, v in attrs.items() if v is not None})
    if text is not None:
        element.text = text
    return element


def _placeholder(parent: Any, index: int) -> None:
    sig = etree.Element(f"{{{NS_DS}}}Signature", nsmap={"ds": NS_DS}, Id="placeholder")
    parent.insert(index, sig)


class _LegacySigner(XMLSigner):
    """Signs with SHA-1, as old identity providers still do (signxml refuses by default)."""

    def check_deprecated_methods(self) -> None:
        return None


class FakeIdp:
    def __init__(self, entity_id: str = ENTITY_ID) -> None:
        self.entity_id = entity_id
        self.key, self.cert, self.cert_pem = new_identity()

    def sign(
        self,
        element: Any,
        *,
        key: Any = None,
        cert: Any = None,
        algorithm: str = "rsa-sha256",
        reference: str | None = None,
    ) -> Any:
        """Sign ``element`` in place of a placeholder (enveloped, reference to its ID); returns the signed copy."""
        signer = (_LegacySigner if algorithm == "rsa-sha1" else XMLSigner)(
            signature_algorithm=SignatureMethod.RSA_SHA1 if algorithm == "rsa-sha1" else SignatureMethod.RSA_SHA256,
            digest_algorithm=DigestAlgorithm.SHA1 if algorithm == "rsa-sha1" else DigestAlgorithm.SHA256,
            c14n_algorithm=CanonicalizationMethod.EXCLUSIVE_XML_CANONICALIZATION_1_0,
        )
        cert_pem = (cert or self.cert).public_bytes(serialization.Encoding.PEM).decode("ascii")
        return signer.sign(
            element,
            key=key or self.key,
            cert=cert_pem,
            reference_uri=reference or element.get("ID"),
            id_attribute="ID",
        )

    def assertion(
        self,
        *,
        request_id: str | None,
        acs_url: str,
        audience: str,
        name_id: str = "jane@example.test",
        name_id_format: str = NAMEID_FORMATS["email"],
        issuer: str | None = None,
        attributes: dict[str, str] | None = None,
        now: dt.datetime | None = None,
        not_before: dt.datetime | None = None,
        not_on_or_after: dt.datetime | None = None,
        scd_not_on_or_after: dt.datetime | None = None,
        recipient: str | None = None,
        assertion_id: str | None = None,
        signed: bool = True,
        **sign_kw: Any,
    ) -> Any:
        now = now or dt.datetime.now(dt.UTC)
        nsmap = {"saml": NS_SAML, "xs": "http://www.w3.org/2001/XMLSchema"}
        assertion = etree.Element(
            f"{{{NS_SAML}}}Assertion",
            nsmap=nsmap,
            ID=assertion_id or f"_a{uuid.uuid4().hex}",
            Version="2.0",
            IssueInstant=_iso(now),
        )
        _el(assertion, NS_SAML, "Issuer", issuer or self.entity_id)
        subject = _el(assertion, NS_SAML, "Subject")
        _el(subject, NS_SAML, "NameID", name_id, Format=name_id_format)
        confirmation = _el(subject, NS_SAML, "SubjectConfirmation", Method=CM_BEARER)
        _el(
            confirmation,
            NS_SAML,
            "SubjectConfirmationData",
            InResponseTo=request_id,
            NotOnOrAfter=_iso(scd_not_on_or_after or now + dt.timedelta(minutes=5)),
            Recipient=recipient or acs_url,
        )
        conditions = _el(
            assertion,
            NS_SAML,
            "Conditions",
            NotBefore=_iso(not_before or now - dt.timedelta(seconds=30)),
            NotOnOrAfter=_iso(not_on_or_after or now + dt.timedelta(minutes=5)),
        )
        restriction = _el(conditions, NS_SAML, "AudienceRestriction")
        _el(restriction, NS_SAML, "Audience", audience)
        statement = _el(assertion, NS_SAML, "AuthnStatement", AuthnInstant=_iso(now), SessionIndex="_s1")
        context = _el(statement, NS_SAML, "AuthnContext")
        _el(
            context,
            NS_SAML,
            "AuthnContextClassRef",
            "urn:oasis:names:tc:SAML:2.0:ac:classes:PasswordProtectedTransport",
        )
        if attributes:
            attribute_statement = _el(assertion, NS_SAML, "AttributeStatement")
            for name, value in attributes.items():
                attribute = _el(attribute_statement, NS_SAML, "Attribute", Name=name)
                _el(attribute, NS_SAML, "AttributeValue", value)
        if not signed:
            return assertion
        _placeholder(assertion, 1)  # schema order: after Issuer
        return self.sign(assertion, **sign_kw)

    def response(
        self,
        assertion: Any | list[Any] | None,
        *,
        acs_url: str,
        request_id: str | None,
        destination: str | None = None,
        issuer: str | None = None,
        status: str = STATUS_SUCCESS,
        sign_response: bool = False,
        now: dt.datetime | None = None,
        **sign_kw: Any,
    ) -> Any:
        now = now or dt.datetime.now(dt.UTC)
        response = etree.Element(
            f"{{{NS_SAMLP}}}Response",
            nsmap={"samlp": NS_SAMLP, "saml": NS_SAML},
            ID=f"_r{uuid.uuid4().hex}",
            Version="2.0",
            IssueInstant=_iso(now),
            Destination=destination or acs_url,
            **({"InResponseTo": request_id} if request_id else {}),
        )
        _el(response, NS_SAML, "Issuer", issuer or self.entity_id)
        status_el = _el(response, NS_SAMLP, "Status")
        _el(status_el, NS_SAMLP, "StatusCode", Value=status)
        for item in assertion if isinstance(assertion, list) else [assertion] if assertion is not None else []:
            response.append(item)
        if not sign_response:
            return response
        _placeholder(response, 1)
        return self.sign(response, **sign_kw)


def to_bytes(element: Any) -> bytes:
    return bytes(etree.tostring(element))


def encode(element_or_bytes: Any) -> str:
    data = element_or_bytes if isinstance(element_or_bytes, bytes) else to_bytes(element_or_bytes)
    return base64.b64encode(data).decode("ascii")
