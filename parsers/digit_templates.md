# Digit template provenance

This reference strip was generated from the live Mega Greninja ex (116/086)
capture committed in `tests/fixtures/ligapokemon/`.

Source files:
- `tests/fixtures/ligapokemon/greninja_116_precocss.html`
- `tests/fixtures/ligapokemon/greninja_116_sprite.jpg`

Reference order:
- Digits `0` through `9`, left to right.

Regeneration:

```bash
python tools/build_digit_templates.py \
  --html tests/fixtures/ligapokemon/greninja_116_precocss.html \
  --sprite tests/fixtures/ligapokemon/greninja_116_sprite.jpg \
  --output parsers/digit_templates.png \
  --prices 843,00 846,50 849,89 849,90 850,00 871,39 879,75 899,00 899,75 949,99 950,29 982,79 999,00 999,90 999,90 999,99
```

If the site changes its font or sprite layout, recapture HTML and sprite from
the same session, decode the `precoCss` listings again, and rebuild this strip
with the new price sequence.
