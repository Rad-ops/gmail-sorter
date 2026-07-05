# Changelog

## 0.2.0 - 2026-07-05

- Added apply-stage progress logging for trash and label/archive writes.
- Added explicit Gmail HTTP timeouts through `httplib2.Http(timeout=...)`.
- Added clearer retry diagnostics with attempt counts and Gmail error text.
- Added `--apply-progress-every`, `--http-timeout`, and `--version`.
- Corrected trash apply status output so it reports messages with planned trash actions instead of the full decision set.
- Documented the safer all-years trash resume command without `--refresh-existing`.

## 0.1.0 - 2026-07-04

- Added staged Gmail classification, reporting, label/archive/trash application, resumable progress files, manifests, dashboard reporting, unsubscribe extraction, attachment review, and high-confidence promotional trash scoring.
