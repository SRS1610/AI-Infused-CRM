# E-invoicing (`account_einvoice`)

Sends customer invoices and credit notes as **UBL 2.1** documents following **Peppol BIS Billing 3.0**
(EN 16931), validated with the official rules, through a Peppol access point; received documents become draft
vendor bills. Data model: [ERD_PHASE_2.md §5](../architecture/ERD_PHASE_2.md).

## Setup

- **Company** (*Settings → Companies*): VAT number, address and country, *Peppol scheme* and *Peppol
  address* (e.g. `9957` + `FR23334175221`, or `0088` + a GLN), *Company registry number* (optional), and
  *Access point* (only the mock is shipped).
- **Customers**: *Peppol scheme* and *Peppol address* on the contact. When they are empty and the customer
  has a VAT number from FR, BE, DE, NL, AT, ES or IT, the address is derived from it (schemes 9957, 9925,
  9930, 9944, 9914, 9920, 0211).
- **Bank account**: the IBAN (and BIC) of the company's first bank journal with a bank account in the
  invoice currency becomes the payment instructions (SEPA credit transfer for EUR).
- **Taxes**: each tax can set its *E-invoice tax category* (UNCL5305: S, Z, E, AE, K, G, O, L, M), an
  exemption reason code (VATEX) and text. Without one, the category is derived: rate > 0 → S; 0 % taxes
  named reverse charge → AE, intra-EU → K, export → G, exempt (or no tax) → E, otherwise Z. The charts' 0 %
  taxes are tagged at installation (EU chart: intra-EU supply K / VATEX-EU-IC, export G / VATEX-EU-G, reverse
  charge AE).

## Sending

On a posted customer invoice or credit note, **Send via Peppol**:

1. the UBL document is generated (Invoice 380 or CreditNote 381; CustomizationID
   `urn:cen.eu:en16931:2017#compliant#urn:fdc:peppol.eu:2017:poacc:billing:3.0`, ProfileID
   `urn:fdc:peppol.eu:2017:poacc:billing:01:1.0`; endpoints with scheme ids, legal entity and VAT scheme,
   payment means and terms, buyer reference, billing reference of credit notes, delivery country for
   intra-community supplies, VAT breakdown per category and rate with exemption reasons, totals, lines with
   their classified tax category; a line discount is stated as a price allowance; an invoice in another
   currency also gives the VAT total in the accounting currency);
2. it is **validated** with the official artefacts (below); a document with a *fatal* finding is not sent —
   the e-invoice record shows the findings;
3. it is handed to the company's access point; the record (*Accounting → Customers → E-invoices*) goes
   *Sent*, then *Delivered* or *Rejected* when the scheduled poll (*Peppol: transmission status and inbound
   documents*, every 20 minutes) asks the access point. **Validate and send again** retries a document in
   error or rejected.

Validation findings are stored structured (`rule_id`, `flag`, `location`, `message`, `source`) on
`account.edi_document.errors`, for people and for the mapping-error explanations of sub-phase 2.6.

## Validation artefacts (2-h)

`backend/modules/account_einvoice/validation/` vendors, with their licences (see its README and NOTICE):

- the **OASIS UBL 2.1** Invoice and CreditNote schemas (unmodified), validated with `lxml`;
- the **CEN EN 16931** UBL rules as published compiled XSLT (CEN/TC 434, EUPL 1.2): BR-*, BR-CO-*,
  BR-S/Z/E/AE/IC/G-*, code lists;
- the **OpenPeppol BIS Billing 3.0** schematron (PEPPOL-EN16931-R*, PEPPOL-COMMON-*), compiled to XSLT with the
  ISO schematron skeleton.

The XSLT 2.0 rules run with **Saxon-HE** (`saxonche`), compiled once per process. Tests generate invoices
and credit notes for categories S, E, K (intra-EU), G (export), mixed rates, discounts, negative lines and a
foreign currency, and assert zero `fatal` findings; they also check that a broken document is reported with
the right rule ids. To update the rules, follow `validation/README.md`.

## Receiving

The poll collects documents addressed to the company's Peppol address: each becomes a **draft vendor bill**
(or refund) with the received XML attached. The supplier is found by Peppol address, then VAT number, then
exact name (created when unknown); lines keep description, quantity and net price; taxes are the company's
purchase taxes of the same rate (reverse-charge and intra-EU categories use the reverse-charge tax);
document-level allowances and charges become lines. Unreadable documents are logged as e-invoices in error.

## Access points

`modules.account_einvoice.access_point` defines the adapter interface — `send(document) → transmission id`,
`status(transmission id)`, `receive() → inbound documents` — and ships a **mock** access point whose
"network" is a table (*Accounting → Configuration → Peppol network (mock)*): sending records the document,
the first status check delivers it (an address containing `reject` refuses it), and the receiving company
collects it. Two companies of the same database can exchange invoices end to end; the demo sends an invoice
from the EU demo company to the US demo company, which receives it as a draft bill.

## Limits

- Only the mock access point is shipped; a real one (certificate, SMP lookup, AS4) is an adapter to add.
- Profile `billing:01:1.0` only; national CIUS rules (XRechnung, …) are not checked.
- One VAT category per line (a tax group adds up its rates); withholding taxes and invoice-level discounts
  are not generated.
