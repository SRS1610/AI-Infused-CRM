"""SAML response validation without a database: signatures, wrapping, conditions, hardened parsing."""

from __future__ import annotations

import base64
import copy
import dataclasses
import datetime as dt
import urllib.parse
import zlib
from typing import Any

import pytest
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import padding
from lxml import etree

from modules.auth_saml import saml
from modules.auth_saml.tests.idp import FakeIdp, new_identity, to_bytes

ACS = "https://erp.example.test/api/v1/auth/saml/7/acs"
SP = "https://erp.example.test/api/v1/auth/saml/7/metadata"
REQ = "_request-1"


@pytest.fixture(scope="module")
def idp() -> FakeIdp:
    return FakeIdp()


@pytest.fixture(scope="module")
def config(idp: FakeIdp) -> saml.SpConfig:
    return saml.SpConfig(idp_entity_id=idp.entity_id, idp_certificates=[idp.cert], sp_entity_id=SP, acs_url=ACS)


def _assertion(idp: FakeIdp, **kw: Any) -> Any:
    kw.setdefault("request_id", REQ)
    return idp.assertion(acs_url=ACS, audience=kw.pop("audience", SP), **kw)


def _response(idp: FakeIdp, assertion: Any = None, **kw: Any) -> bytes:
    kw.setdefault("request_id", REQ)
    return to_bytes(idp.response(_assertion(idp) if assertion is None else assertion, acs_url=ACS, **kw))


def _validate(xml: bytes, config: saml.SpConfig, **kw: Any) -> saml.ValidatedAssertion:
    kw.setdefault("expected_request_ids", [REQ])
    return saml.validate_response(xml, config, **kw)


def _refused(xml: bytes, config: saml.SpConfig, code: str, **kw: Any) -> saml.SamlError:
    with pytest.raises(saml.SamlError) as info:
        _validate(xml, config, **kw)
    assert info.value.code == code, (info.value.code, info.value.message)
    return info.value


# -- accepted responses ---------------------------------------------------------------------------------


def test_signed_assertion_is_accepted(idp: FakeIdp, config: saml.SpConfig) -> None:
    attrs = {"displayName": "Jane Doe", "mail": "Jane@Example.test"}
    result = _validate(_response(idp, _assertion(idp, attributes=attrs)), config)
    assert result.name_id == "jane@example.test"
    assert result.in_response_to == REQ and result.issuer == idp.entity_id
    assert result.attribute("displayName") == "Jane Doe" and result.attribute("missing") is None
    assert result.session_index == "_s1"
    assert result.valid_until > dt.datetime.now(dt.UTC)


def test_signed_response_and_signed_assertion_are_accepted(idp: FakeIdp, config: saml.SpConfig) -> None:
    assert _validate(_response(idp, sign_response=True), config).name_id == "jane@example.test"


def test_response_signature_alone_needs_the_provider_to_allow_it(idp: FakeIdp, config: saml.SpConfig) -> None:
    xml = _response(idp, _assertion(idp, signed=False), sign_response=True)
    _refused(xml, config, "assertion_unsigned")
    relaxed = dataclasses.replace(config, want_assertions_signed=False)
    assert _validate(xml, relaxed).name_id == "jane@example.test"


def test_any_configured_certificate_verifies_during_a_rotation(idp: FakeIdp, config: saml.SpConfig) -> None:
    _, old_cert, _ = new_identity("Old IdP key")
    rotating = dataclasses.replace(config, idp_certificates=[old_cert, idp.cert])
    assert _validate(_response(idp), rotating).name_id


# -- signatures -----------------------------------------------------------------------------------------


def test_unsigned_response_is_refused(idp: FakeIdp, config: saml.SpConfig) -> None:
    _refused(_response(idp, _assertion(idp, signed=False)), config, "unsigned")


def test_signature_by_another_key_is_refused(idp: FakeIdp, config: saml.SpConfig) -> None:
    attacker_key, attacker_cert, _ = new_identity("Attacker")
    # the attacker's certificate travels in the message's KeyInfo: it must not be trusted
    forged = _assertion(idp, key=attacker_key, cert=attacker_cert)
    assert forged.find(".//{http://www.w3.org/2000/09/xmldsig#}X509Certificate") is not None
    _refused(_response(idp, forged), config, "signature_invalid")
    _refused(_response(idp, forged), dataclasses.replace(config, idp_certificates=[]), "no_certificate")


def test_tampered_assertion_is_refused(idp: FakeIdp, config: saml.SpConfig) -> None:
    assertion = _assertion(idp)
    assertion.find(f"{saml.T_SUBJECT}/{saml.T_NAMEID}").text = "admin@example.test"
    _refused(_response(idp, assertion), config, "signature_invalid")


def test_malformed_signature_elements_fail_closed(idp: FakeIdp, config: saml.SpConfig) -> None:
    assertion = _assertion(idp)
    assertion.find(f"{saml.T_SIGNATURE}/{{{saml.NS_DS}}}SignatureValue").text = None
    _refused(_response(idp, assertion), config, "signature_invalid")
    stripped = _assertion(idp)
    signature = stripped.find(saml.T_SIGNATURE)
    signature.remove(signature.find(f"{{{saml.NS_DS}}}SignedInfo"))
    _refused(_response(idp, stripped), config, "signature_invalid")


def test_sha1_signatures_are_refused(idp: FakeIdp, config: saml.SpConfig) -> None:
    _refused(_response(idp, _assertion(idp, algorithm="rsa-sha1")), config, "signature_invalid")


def test_signature_must_cover_the_element_it_sits_in(idp: FakeIdp, config: saml.SpConfig) -> None:
    # a valid signature in the response that covers the (unsigned) assertion instead of the response itself
    response = idp.response(_assertion(idp, signed=False), acs_url=ACS, request_id=REQ)
    assertion_id = response.find(saml.T_ASSERTION).get("ID")
    response.insert(1, etree.Element(f"{{{saml.NS_DS}}}Signature", nsmap={"ds": saml.NS_DS}, Id="placeholder"))
    _refused(to_bytes(idp.sign(response, reference=assertion_id)), config, "signature_reference")
    # a valid assertion signature copied into the response does not verify there
    assertion = _assertion(idp)
    moved = idp.response(assertion, acs_url=ACS, request_id=REQ)
    moved.insert(1, copy.deepcopy(assertion.find(saml.T_SIGNATURE)))
    _refused(to_bytes(moved), config, "signature_invalid")


# -- signature wrapping -----------------------------------------------------------------------------------


def test_injected_unsigned_assertion_next_to_a_signed_one_is_refused(idp: FakeIdp, config: saml.SpConfig) -> None:
    evil = _assertion(idp, name_id="admin@example.test", signed=False)
    signed = _assertion(idp)
    _refused(_response(idp, [evil, signed]), config, "assertion_count")
    _refused(_response(idp, [signed, evil]), config, "assertion_count")


def test_signed_assertion_hidden_inside_an_unsigned_one_is_refused(idp: FakeIdp, config: saml.SpConfig) -> None:
    signed = _assertion(idp)
    evil = _assertion(idp, name_id="admin@example.test", signed=False, assertion_id=signed.get("ID"))
    # the original signature is copied into the evil assertion, the signed one tucked away in Advice
    evil.insert(1, copy.deepcopy(signed.find(saml.T_SIGNATURE)))
    advice = etree.SubElement(evil, f"{{{saml.NS_SAML}}}Advice")
    advice.append(signed)
    _refused(_response(idp, evil), config, "assertion_count")


def test_signed_assertion_moved_into_response_extensions_is_refused(idp: FakeIdp, config: saml.SpConfig) -> None:
    signed = _assertion(idp)
    evil = _assertion(idp, name_id="admin@example.test", signed=False, assertion_id=signed.get("ID"))
    evil.insert(1, copy.deepcopy(signed.find(saml.T_SIGNATURE)))
    response = idp.response(evil, acs_url=ACS, request_id=REQ)
    extensions = etree.Element(f"{{{saml.NS_SAMLP}}}Extensions")
    extensions.append(signed)
    response.insert(1, extensions)
    _refused(to_bytes(response), config, "assertion_count")
    # without the hidden original, the copied signature does not match the evil content
    response.remove(extensions)
    _refused(to_bytes(response), config, "signature_invalid")


def test_signed_response_wrapping_an_extra_assertion_is_refused(idp: FakeIdp, config: saml.SpConfig) -> None:
    response = idp.response(_assertion(idp), acs_url=ACS, request_id=REQ, sign_response=True)
    response.append(_assertion(idp, name_id="admin@example.test", signed=False))
    _refused(to_bytes(response), config, "assertion_count")


def test_only_the_signed_data_is_read(idp: FakeIdp, config: saml.SpConfig) -> None:
    # comments inside the NameID are not part of the canonical signed text: the full value is read
    assertion = _assertion(idp, name_id="jane@example.test.evil.test", signed=False)
    name_id = assertion.find(f"{saml.T_SUBJECT}/{saml.T_NAMEID}")
    name_id.text = "jane@example.test"
    name_id.append(etree.Comment("x"))
    name_id[-1].tail = ".evil.test"
    _placeholder = etree.Element(f"{{{saml.NS_DS}}}Signature", nsmap={"ds": saml.NS_DS}, Id="placeholder")
    assertion.insert(1, _placeholder)
    signed = idp.sign(assertion)
    assert _validate(_response(idp, signed), config).name_id == "jane@example.test.evil.test"


# -- protocol checks ---------------------------------------------------------------------------------------


def test_wrong_audience_is_refused(idp: FakeIdp, config: saml.SpConfig) -> None:
    _refused(_response(idp, _assertion(idp, audience="https://other.example.test/sp")), config, "audience")


def test_every_audience_restriction_must_name_this_sp(idp: FakeIdp, config: saml.SpConfig) -> None:
    assertion = _assertion(idp, signed=False)
    conditions = assertion.find(saml.T_CONDITIONS)
    other = etree.SubElement(conditions, saml.T_AUDIENCE_RESTRICTION)
    etree.SubElement(other, saml.T_AUDIENCE).text = "https://other.example.test/sp"
    assertion.insert(1, etree.Element(f"{{{saml.NS_DS}}}Signature", nsmap={"ds": saml.NS_DS}, Id="placeholder"))
    _refused(_response(idp, idp.sign(assertion)), config, "audience")
    no_restriction = _assertion(idp, signed=False)
    no_restriction.find(saml.T_CONDITIONS).remove(
        no_restriction.find(f"{saml.T_CONDITIONS}/{saml.T_AUDIENCE_RESTRICTION}")
    )
    no_restriction.insert(1, etree.Element(f"{{{saml.NS_DS}}}Signature", nsmap={"ds": saml.NS_DS}, Id="placeholder"))
    _refused(_response(idp, idp.sign(no_restriction)), config, "audience")


def test_wrong_destination_is_refused(idp: FakeIdp, config: saml.SpConfig) -> None:
    _refused(_response(idp, destination="https://evil.example.test/acs"), config, "destination")


def test_signed_response_must_name_its_destination(idp: FakeIdp, config: saml.SpConfig) -> None:
    response = idp.response(_assertion(idp), acs_url=ACS, request_id=REQ)
    del response.attrib["Destination"]
    response.insert(1, etree.Element(f"{{{saml.NS_DS}}}Signature", nsmap={"ds": saml.NS_DS}, Id="placeholder"))
    _refused(to_bytes(idp.sign(response)), config, "destination")


def test_wrong_recipient_is_refused(idp: FakeIdp, config: saml.SpConfig) -> None:
    _refused(_response(idp, _assertion(idp, recipient="https://evil.example.test/acs")), config, "recipient")


def test_expired_assertion_is_refused(idp: FakeIdp, config: saml.SpConfig) -> None:
    past = dt.datetime.now(dt.UTC) - dt.timedelta(hours=1)
    _refused(_response(idp, _assertion(idp, now=past)), config, "expired")
    # the bearer confirmation expired although the conditions still hold
    soon = dt.datetime.now(dt.UTC) - dt.timedelta(minutes=10)
    _refused(_response(idp, _assertion(idp, scd_not_on_or_after=soon)), config, "expired")


def test_clock_skew_is_tolerated(idp: FakeIdp, config: saml.SpConfig) -> None:
    now = dt.datetime.now(dt.UTC)
    late = _assertion(idp, not_on_or_after=now - dt.timedelta(seconds=60))
    assert _validate(_response(idp, late), config).name_id
    _refused(_response(idp, late), dataclasses.replace(config, clock_skew=0), "expired")


def test_assertion_not_yet_valid_is_refused(idp: FakeIdp, config: saml.SpConfig) -> None:
    future = dt.datetime.now(dt.UTC) + dt.timedelta(minutes=10)
    _refused(_response(idp, _assertion(idp, not_before=future)), config, "not_yet_valid")


def test_issuer_mismatch_is_refused(idp: FakeIdp, config: saml.SpConfig) -> None:
    _refused(_response(idp, _assertion(idp, issuer="https://evil.example.test")), config, "issuer")
    _refused(_response(idp, issuer="https://evil.example.test"), config, "issuer")


def test_unexpected_in_response_to_is_refused(idp: FakeIdp, config: saml.SpConfig) -> None:
    _refused(_response(idp), config, "in_response_to", expected_request_ids=["_another"])
    _refused(_response(idp, request_id="_mismatch"), config, "in_response_to")


def test_unsolicited_response_is_refused(idp: FakeIdp, config: saml.SpConfig) -> None:
    _refused(_response(idp, _assertion(idp, request_id=None), request_id=None), config, "unsolicited")
    _refused(_response(idp, _assertion(idp, request_id=None), request_id=None), config, "unsolicited",
             expected_request_ids=None)  # fmt: skip


def test_failed_status_is_refused(idp: FakeIdp, config: saml.SpConfig) -> None:
    _refused(_response(idp, status="urn:oasis:names:tc:SAML:2.0:status:Requester"), config, "status")


def test_transient_name_id_is_refused(idp: FakeIdp, config: saml.SpConfig) -> None:
    assertion = _assertion(idp, name_id="_t123", name_id_format=saml.NAMEID_TRANSIENT)
    _refused(_response(idp, assertion), config, "transient_nameid")


def test_encrypted_assertion_is_refused_with_a_clear_message(idp: FakeIdp, config: saml.SpConfig) -> None:
    response = idp.response(None, acs_url=ACS, request_id=REQ)
    etree.SubElement(response, saml.T_ENCRYPTED_ASSERTION)
    error = _refused(to_bytes(response), config, "encrypted")
    assert "Encrypted assertions are not supported" in error.message


def test_missing_elements_are_refused(idp: FakeIdp, config: saml.SpConfig) -> None:
    _refused(_response(idp, []), config, "assertion_count")
    for tag, code in (
        (saml.T_CONDITIONS, "conditions"),
        (saml.T_SUBJECT, "subject"),
        (saml.T_AUTHN_STATEMENT, "authn_statement"),
    ):
        assertion = _assertion(idp, signed=False)
        assertion.remove(assertion.find(tag))
        assertion.insert(1, etree.Element(f"{{{saml.NS_DS}}}Signature", nsmap={"ds": saml.NS_DS}, Id="placeholder"))
        _refused(_response(idp, idp.sign(assertion)), config, code)


def _drop_id(a: Any) -> None:
    a.attrib["ID"] = ""


def _subject_confirmation(a: Any) -> Any:
    return a.find(f"{saml.T_SUBJECT}/{saml.T_SUBJECT_CONFIRMATION}")


def _scd(a: Any) -> Any:
    return a.find(f"{saml.T_SUBJECT}/{saml.T_SUBJECT_CONFIRMATION}/{saml.T_SUBJECT_CONFIRMATION_DATA}")


def _set_session_expired(a: Any) -> None:
    a.find(saml.T_AUTHN_STATEMENT).set("SessionNotOnOrAfter", "2020-01-01T00:00:00Z")


def _future_scd(a: Any) -> None:
    _scd(a).set("NotBefore", (dt.datetime.now(dt.UTC) + dt.timedelta(hours=1)).strftime("%Y-%m-%dT%H:%M:%SZ"))


def _holder_of_key(a: Any) -> None:
    _subject_confirmation(a).set("Method", "urn:oasis:names:tc:SAML:2.0:cm:holder-of-key")


def _no_confirmation_data(a: Any) -> None:
    _subject_confirmation(a).remove(_scd(a))


@pytest.mark.parametrize(
    ("mutate", "code"),
    [
        (_drop_id, "assertion_id"),
        (lambda a: a.set("Version", "1.1"), "version"),
        (lambda a: setattr(a.find(f"{saml.T_SUBJECT}/{saml.T_NAMEID}"), "text", "  "), "subject"),
        (_set_session_expired, "expired"),
        (_future_scd, "not_yet_valid"),
        (_holder_of_key, "confirmation"),
        (_no_confirmation_data, "confirmation"),
    ],
)
def test_malformed_assertions_are_refused(idp: FakeIdp, config: saml.SpConfig, mutate: Any, code: str) -> None:
    assertion = _assertion(idp, signed=False)
    mutate(assertion)
    if assertion.get("ID"):
        assertion.insert(1, etree.Element(f"{{{saml.NS_DS}}}Signature", nsmap={"ds": saml.NS_DS}, Id="placeholder"))
        assertion = idp.sign(assertion)
    _refused(_response(idp, assertion), config, code)


def test_attribute_names(idp: FakeIdp, config: saml.SpConfig) -> None:
    assertion = _assertion(idp, signed=False, attributes={"urn:oid:0.9.2342.19200300.100.1.3": "jane@example.test"})
    statement = assertion.find(saml.T_ATTRIBUTE_STATEMENT)
    statement[0].set("FriendlyName", "mail")
    nameless = etree.SubElement(statement, saml.T_ATTRIBUTE)
    etree.SubElement(nameless, saml.T_ATTRIBUTE_VALUE).text = "ignored"
    assertion.insert(1, etree.Element(f"{{{saml.NS_DS}}}Signature", nsmap={"ds": saml.NS_DS}, Id="placeholder"))
    result = _validate(_response(idp, idp.sign(assertion)), config)
    assert result.attribute("mail") == result.attribute("urn:oid:0.9.2342.19200300.100.1.3") == "jane@example.test"
    assert set(result.attributes) == {"mail", "urn:oid:0.9.2342.19200300.100.1.3"}


def test_not_a_saml_response_is_refused(config: saml.SpConfig) -> None:
    with pytest.raises(saml.SamlError) as info:
        saml.validate_response(b"<foo/>", config)
    assert info.value.code == "not_a_response"
    with pytest.raises(saml.SamlError) as info:
        saml.validate_response(f'<samlp:Response xmlns:samlp="{saml.NS_SAMLP}" Version="1.1"/>'.encode(), config)
    assert info.value.code == "version"
    with pytest.raises(saml.SamlError) as info:
        saml.validate_response(b"<unclosed>", config)
    assert info.value.code == "malformed"
    with pytest.raises(saml.SamlError) as info:
        saml.validate_response(b"<a>" + b"x" * saml.MAX_RESPONSE_BYTES + b"</a>", config)
    assert info.value.code == "too_large"


# -- hardened parsing ----------------------------------------------------------------------------------------

XXE = b"""<?xml version="1.0"?>
<!DOCTYPE r [<!ENTITY xxe SYSTEM "file:///etc/passwd">]>
<samlp:Response xmlns:samlp="urn:oasis:names:tc:SAML:2.0:protocol" Version="2.0">&xxe;</samlp:Response>"""

LAUGHS = b"""<?xml version="1.0"?>
<!DOCTYPE lolz [<!ENTITY lol "lol"><!ENTITY lol2 "&lol;&lol;&lol;&lol;&lol;&lol;&lol;&lol;&lol;&lol;">
<!ENTITY lol3 "&lol2;&lol2;&lol2;&lol2;&lol2;&lol2;&lol2;&lol2;&lol2;&lol2;">]>
<samlp:Response xmlns:samlp="urn:oasis:names:tc:SAML:2.0:protocol" Version="2.0">&lol3;</samlp:Response>"""


@pytest.mark.parametrize("payload", [XXE, LAUGHS, b'<!doctype r SYSTEM "http://evil.example.test/x.dtd"><r/>'])
def test_dtd_and_entities_are_refused(payload: bytes, config: saml.SpConfig) -> None:
    with pytest.raises(saml.SamlError) as info:
        saml.validate_response(payload, config)
    assert info.value.code == "dtd"


def test_dtd_is_refused_even_when_not_spotted_in_the_raw_bytes() -> None:
    # UTF-16 hides the marker from the byte scan; the parsed document still reveals the DTD
    payload = '<?xml version="1.0" encoding="UTF-16"?><!DOCTYPE r [<!ENTITY a "b">]><r>&a;</r>'.encode("utf-16")
    with pytest.raises(saml.SamlError) as info:
        saml.parse_xml(payload)
    assert info.value.code == "dtd"


def test_post_binding_decoding() -> None:
    assert saml.decode_post_response(base64.b64encode(b"<x/>").decode() + "\n") == b"<x/>"
    for bad, code in (
        ("", "missing"),
        ("not base64!", "malformed"),
        ("A" * (saml.MAX_RESPONSE_BYTES * 2), "too_large"),
    ):
        with pytest.raises(saml.SamlError) as info:
            saml.decode_post_response(bad)
        assert info.value.code == code


def test_invalid_times_are_refused(idp: FakeIdp, config: saml.SpConfig) -> None:
    assertion = _assertion(idp, signed=False)
    assertion.find(saml.T_CONDITIONS).set("NotBefore", "yesterday")
    assertion.insert(1, etree.Element(f"{{{saml.NS_DS}}}Signature", nsmap={"ds": saml.NS_DS}, Id="placeholder"))
    _refused(_response(idp, idp.sign(assertion)), config, "bad_time")


# -- outgoing messages --------------------------------------------------------------------------------------


def _inflate(url: str) -> tuple[Any, dict[str, str]]:
    query = dict(urllib.parse.parse_qsl(urllib.parse.urlsplit(url).query))
    xml = zlib.decompress(base64.b64decode(query["SAMLRequest"]), -15)
    return etree.fromstring(xml), query


def test_authn_request_and_redirect_binding() -> None:
    now = dt.datetime(2026, 11, 10, 12, 0, tzinfo=dt.UTC)
    request_id = saml.new_message_id()
    assert request_id.startswith("_") and len(request_id) == 41
    xml = saml.build_authn_request(
        request_id=request_id,
        sp_entity_id=SP,
        acs_url=ACS,
        destination="https://idp.example.test/sso?tenant=1",
        name_id_format=saml.NAMEID_FORMATS["email"],
        now=now,
    )
    url = saml.redirect_url("https://idp.example.test/sso?tenant=1", xml, "relay&state")
    assert url.startswith("https://idp.example.test/sso?tenant=1&SAMLRequest=")
    request, query = _inflate(url)
    assert request.get("ID") == request_id and request.get("IssueInstant") == "2026-11-10T12:00:00Z"
    assert request.get("AssertionConsumerServiceURL") == ACS and request.get("ProtocolBinding") == saml.BINDING_POST
    assert request.find(saml.T_ISSUER).text == SP
    assert request.find(f"{{{saml.NS_SAMLP}}}NameIDPolicy").get("Format") == saml.NAMEID_FORMATS["email"]
    assert query["RelayState"] == "relay&state" and "Signature" not in query


@pytest.mark.parametrize("kind", ["rsa", "ec"])
def test_redirect_binding_signature_verifies_with_the_sp_certificate(kind: str) -> None:
    from cryptography.hazmat.primitives.asymmetric import ec

    if kind == "rsa":
        key_pem, cert_pem = saml.generate_sp_keypair("SP")
        key = saml.load_private_key(key_pem)
        public = saml.load_certificates(cert_pem)[0].public_key()
        assert saml.key_matches_certificate(key, saml.load_certificates(cert_pem)[0])
    else:
        key = ec.generate_private_key(ec.SECP256R1())
        public = key.public_key()
    xml = saml.build_authn_request(
        request_id="_r", sp_entity_id=SP, acs_url=ACS, destination="https://idp/sso", name_id_format=None,
        now=dt.datetime.now(dt.UTC),
    )  # fmt: skip
    url = saml.redirect_url("https://idp/sso", xml, "rs", signing_key=key)
    raw_query = urllib.parse.urlsplit(url).query
    signed_part, _, signature = raw_query.rpartition("&Signature=")
    sig = base64.b64decode(urllib.parse.unquote(signature))
    query = dict(urllib.parse.parse_qsl(raw_query))
    assert query["SigAlg"] == saml.REDIRECT_SIG_ALGS[kind]
    if kind == "rsa":
        public.verify(sig, signed_part.encode(), padding.PKCS1v15(), hashes.SHA256())  # type: ignore[union-attr]
    else:
        public.verify(sig, signed_part.encode(), ec.ECDSA(hashes.SHA256()))  # type: ignore[call-arg]


def test_sp_metadata() -> None:
    _, cert_pem = saml.generate_sp_keypair("SP")
    cert = saml.load_certificates(cert_pem)[0]
    doc = etree.fromstring(
        saml.sp_metadata(
            sp_entity_id=SP,
            acs_url=ACS,
            name_id_format=saml.NAMEID_FORMATS["persistent"],
            want_assertions_signed=True,
            signing_certificate=cert,
        )
    )
    md = {"md": saml.NS_MD, "ds": saml.NS_DS}
    assert doc.get("entityID") == SP
    descriptor = doc.find("md:SPSSODescriptor", md)
    assert descriptor.get("AuthnRequestsSigned") == "true" and descriptor.get("WantAssertionsSigned") == "true"
    assert doc.find(".//md:AssertionConsumerService", md).get("Location") == ACS
    assert doc.find(".//ds:X509Certificate", md).text == saml.certificate_body(cert)
    plain = etree.fromstring(
        saml.sp_metadata(
            sp_entity_id=SP, acs_url=ACS, name_id_format=None, want_assertions_signed=False, signing_certificate=None
        )
    )
    assert plain.find("md:SPSSODescriptor", md).get("AuthnRequestsSigned") == "false"
    assert plain.find(".//md:KeyDescriptor", md) is None


def test_certificate_and_key_loading(idp: FakeIdp) -> None:
    assert saml.load_certificates(None) == []
    bundle = idp.cert_pem + new_identity("Next")[2]
    assert len(saml.load_certificates(bundle)) == 2
    body = "".join(line for line in idp.cert_pem.splitlines() if "-----" not in line)
    assert saml.load_certificates(body)[0] == idp.cert  # bare base64 as found in IdP metadata
    with pytest.raises(saml.SamlError):
        saml.load_certificates("-----BEGIN CERTIFICATE-----\nnope\n-----END CERTIFICATE-----")
    with pytest.raises(saml.SamlError):
        saml.load_private_key("not a key")
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import ed25519

    ed = ed25519.Ed25519PrivateKey.generate().private_bytes(
        serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()
    )
    with pytest.raises(saml.SamlError):
        saml.load_private_key(ed.decode())
    other_key, _ = saml.generate_sp_keypair("other")
    assert not saml.key_matches_certificate(saml.load_private_key(other_key), idp.cert)
