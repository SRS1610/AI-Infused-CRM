# Official validation artefacts (vendored)

These files are third-party artefacts copied unmodified (except where stated) so that generated documents can
be validated offline, in tests and before sending (`modules/account_einvoice/validator.py`).

| Path | What | Source | Licence |
|---|---|---|---|
| `xsd/maindoc/UBL-Invoice-2.1.xsd`, `xsd/maindoc/UBL-CreditNote-2.1.xsd`, `xsd/common/*.xsd` | OASIS UBL 2.1 schemas | OASIS UBL 2.1 (`docs.oasis-open.org/ubl/os-UBL-2.1/`), files as redistributed in `github.com/phax/ph-ubl` (`ph-ubl21/src/main/resources/external/schemas/ubl21`) | OASIS copyright notice at the end of each file (copies permitted, unmodified) |
| `xsd/CCTS_CCT_SchemaModule.xsd`, `xsd/xmldsig-core-schema.xsd` | UN/CEFACT CCTS core component types and W3C XML Signature schema, imported by name by the UBL schemas | `github.com/phax/ph-xsds` (`ph-xsds-ccts-cct-schemamodule`, `ph-xsds-xmldsig`) | Apache-2.0 (`licenses/ph-ubl-ph-xsds-Apache-2.0.txt`) |
| `xsd/tessera-ubl-*.xsd` | Tessera entry points: import the namespaces above with their location, then the OASIS document schema | written for Tessera | Apache-2.0 |
| `schematron/CEN-EN16931-UBL.xslt` | CEN/TC 434 EN 16931 validation rules for UBL (compiled XSLT 2.0, "Schematron version 1.3.17 - 2026-10-09") | `github.com/ConnectingEurope/eInvoicing-EN16931`, `ubl/xslt/EN16931-UBL-validation.xslt`, commit `b17b2695e5e3e021945c38658326b78a76f80f84` | EUPL-1.2 (`licenses/CEN-eInvoicing-EN16931-EUPL-1.2.txt`) |
| `schematron/PEPPOL-EN16931-UBL.sch` | OpenPeppol BIS Billing 3.0 rules (hotfix 3.0.22) | `github.com/OpenPEPPOL/peppol-bis-invoice-3`, `rules/sch/`, commit `38d693d71d28d813781323a6ca97324edcc4d239` | OpenPeppol AISBL, published under the EC/CEN agreement allowing derivative works without additional licensing (see `NOTICE.md`) |
| `schematron/PEPPOL-EN16931-UBL.xslt` | the Peppol schematron above compiled to XSLT 2.0 with the ISO skeleton (generated, not edited) | `validator.compile_schematron()` | as the `.sch` |
| `iso-schematron/*.xsl` | ISO Schematron reference implementation (skeleton for XSLT 2 / SVRL) used to compile `.sch` files | `github.com/Schematron/schematron`, `trunk/schematron/code` | MIT (`licenses/ISO-Schematron-skeleton-MIT.txt`) |

## Updating

1. Replace `schematron/CEN-EN16931-UBL.xslt` with the new CEN release and `schematron/PEPPOL-EN16931-UBL.sch`
   with the new Peppol release.
2. Recompile the Peppol rules:

   ```bash
   cd backend
   uv run python -c "from modules.account_einvoice.validator import compile_schematron as c, ROOT; \
     c(ROOT / 'schematron/PEPPOL-EN16931-UBL.sch', ROOT / 'schematron/PEPPOL-EN16931-UBL.xslt')"
   ```

3. Run `uv run pytest modules/account_einvoice` (generated documents must have no `fatal` finding).
