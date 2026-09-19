# Development data policy

The files in `app/data` are unverified development inputs. They exist to make
schema, repository, search, quotation, and conversation scenarios repeatable;
they are not verified Global Detergent Factory commercial data.

`development_data_manifest.json` is the machine-readable provenance record.
Its `production_approved` value is deliberately `false`, and
`business_review_required` is `true`. Startup validation added in the static
repository/startup task must reject this manifest in production until an
authorized business review replaces or approves every unverified value.

## Source and synthetic values

- `company.json` follows the Specification section 8 example and applies the
  contract's nested `contact` normalization. Unknown phone, email, website,
  and address values remain `null`.
- The ten names in `products.json` come from Specification section 10.
- The GDF Floor Cleaner ID, SKU, name, 5 L package, and QAR 25.00 price preserve
  the Specification section 9 example. They remain unverified business data.
- Prices, packaging, identifiers, categories, descriptions, keywords, and
  activation flags selected for the other products are synthetic development
  values. The manifest records these fields for each product.
- `quotation_rules.json` reproduces the Task 11/Specification section 16
  example. Currency, validity, discount, tax, prefix, and terms still require
  business approval; the example does not establish tax law, discount
  authority, or commercial policy.

## Intentionally unavailable facts

The catalog does not supply product instructions, safety information,
ingredients, recommended surfaces, contact times, dilution ratios,
certifications, approved efficacy claims, color, fragrance, or pH. These stay
`null` or empty and must be reported as unavailable. Product names and search
keywords must never be used to infer them.

## Production replacement gate

Before any production deployment, an authorized business owner must review and
replace or explicitly approve the company profile, every product record, every
quotation rule, and every manifest provenance entry. The production catalog
must retain evidence of that review and must not set `production_approved` to
`true` merely to bypass startup validation.
