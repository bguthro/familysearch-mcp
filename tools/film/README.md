# film_pages

Download a range of pages from the microfilm that a record image ark belongs to.

    pip install -e '.[film]'
    python -m tools.film.film_pages ARK --first 10 --last 15 --out ~/film_out \
        [--crop left-parents] [--box x0,y0,x1,y1] [--montage] [--size dist|thumb_p200]

Files are written as `film_<N:05d>.jpg`. `--crop left-parents` keeps the left-page
names column (default box fractions 0.09,0.265,0.335,0.80 of width/height; override
with `--box`). `--montage` (needs `--crop`) pairs consecutive crops side by side with
image-number labels.

## Security limits

- Production only, read-only. The token comes from the Brave session cookie.
- The bearer token is sent only to URLs that pass strict ASCII fullmatch validators
  (`validate_das_url`, `validate_image_url`); metadata calls never follow redirects,
  require a JSON content type and are size capped.
- At most 40 images per run; output under the repository root is refused.
- Output lists only counts and file names, never URLs or tokens.
