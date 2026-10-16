# Contributing

Small fixes, clearer setup instructions and reproducible bug reports are welcome.
Open an issue describing the behavior and your operating system before a large change.
For a fix, fork the repository and open a pull request with what changed and how
it was checked. Use synthetic fixtures. Do not attach personal media, transcripts,
credentials, browser cookies, local paths or private project records.

Original contributions are provided under the repository's MIT License.
Preserve copyright and third-party license notices. Keep changes limited to the
public tool; do not copy a private workspace or its history into this repository.

Before a pull request, stage the intended source files, run
`python3 scripts/verify_release.py --write-manifest`, stage MANIFEST.json, then run
`python3 scripts/verify_release.py`. Describe additional runtime checks you ran.
A passing source check does not establish that an edit or transcription succeeded.
