# Third-Party Notices

JobScout is distributed under the MIT License. The following projects and services remain subject to their own licenses and terms.

## Runtime and tooling

- Hermes Agent — https://github.com/NousResearch/hermes-agent — MIT License. JobScout normally invokes an independently installed Hermes runtime. The tracked optional downstream patch, `patches/hermes-agent-local-fixes.patch`, contains modified Hermes source and test context derived from upstream commit `3a3dd5c5140d76f65a829150b1dece7e72b6552c`. It is disabled by default and is distributed under the upstream MIT terms reproduced below.
- DuckDB — https://github.com/duckdb/duckdb — MIT License. The optional community-source environment installs the pinned Python package declared in `requirements-community.txt`.
- Tectonic — https://github.com/tectonic-typesetting/tectonic — MIT License. The bootstrap downloads a pinned, checksum-verified upstream binary.
- Playwright — https://github.com/microsoft/playwright — Apache License 2.0. Playwright is optional and is installed separately only when a browser adapter requires it.

### Hermes Agent MIT notice

Copyright (c) 2025 Nous Research

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all
copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
SOFTWARE.

## Discovery sources

- ats-scrapers — https://github.com/kalil0321/ats-scrapers — source code is MIT-licensed. No separate license for its hosted dataset was stated when this integration was prepared.
- OpenRoles — https://github.com/datascry/openroles — source code is MIT-licensed; its generated data is licensed under CC BY-SA 4.0: https://creativecommons.org/licenses/by-sa/4.0/

The upstream datasets are fetched as bounded public artifacts at runtime and are not redistributed in this repository. Their records are treated as unverified discovery leads. Users are responsible for complying with upstream data licenses, website terms, and applicable law.

## External services

- Photon — https://photon.codes — optional managed messaging service, governed by its service terms.
- LinkedIn, Dice, Indeed, employer career sites, and applicant-tracking systems are external services governed by their respective terms. JobScout does not bypass authentication, MFA, CAPTCHA, or access controls.

This notice is informational and does not replace the full upstream license texts or service terms.
