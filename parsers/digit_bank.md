# Digit bank provenance (`digit_bank.png`)

`digit_bank.png` is the reference asset the sprite decoder matches against
(issue #12). It is a grayscale atlas: **one row per digit `0`–`9`**, and **one
`8×21` column per distinct rendered bitmap** of that digit. Unused trailing slots
are left white and skipped by the loader.

## Why a bank instead of one template per digit

LigaPokemon's price sprite is a **lossy JPEG, randomised per page load**. The
glyph bitmaps themselves are pixel-stable, but the JPEG re-compression perturbs
each digit position-dependently, so a single committed template only matches the
positions sharing its compression context — on a live page that dropped ~20–25%
of digit crops, and a looser cutoff risked the *wrong* digit (a `9` whose nearest
single template was `6`).

Measured against the live sprite, each digit renders as one of just **two**
pixel-stable bitmaps (e.g. digit `9`'s two variants are ~24 mean-abs-diff apart;
correct matches land at ~0; the nearest cross-digit is >7). Holding **all**
bitmaps per digit and matching by nearest neighbour with a tight cutoff yields,
on leave-one-capture-out over 18 live renders, **100% coverage with zero
misreads**.

## Source

Built from 18 in-session captures of **Mega Greninja ex (116/086)**, ed CRI:

```
https://www.ligapokemon.com.br/?view=cards/card&card=Mega+Greninja+ex%20(116/086)&ed=CRI&num=116
```

The 18 obfuscated (`precoCss`) prices on that card, read once from the rendered
page (the labelling ground truth):

```
829,98 843,00 846,50 849,89 849,90 850,00 871,39 879,75 899,00
899,75 899,90 949,99 950,29 982,79 999,00 999,90 999,90 999,99
```

## Rebuild

1. Capture several renders in-session (the sprite URL is per-load):
   ```
   python tools/capture_page.py "<card url>" --out captures/ --stem greninja --count 18
   ```
2. Read the obfuscated prices off the rendered page into `prices.txt`, one per
   line (e.g. `843,00`).
3. Build the atlas:
   ```
   python tools/build_digit_bank.py --captures captures/ --prices prices.txt \
       --output parsers/digit_bank.png
   ```

`build_digit_bank.py` bootstraps confident digits from the *current* bank, unions
them per listing `lj_id` across captures, and matches each listing to a unique
price in the set — so no per-listing ordering is needed, only the price set.
Capture enough renders that every digit's bitmaps all appear (the build prints
the per-digit count; coverage saturates within a handful of captures).

## When the site changes its font

If LigaPokemon changes the glyph rendering, live crops stop matching, decoding
**fails safe** (the listing is skipped and reported — FRD §10), and the held-out
gate in `tests/test_digit_bank.py` will go red. Re-capture and rebuild. A total
font change defeats the bootstrap; re-seed by hand-labelling one capture.
