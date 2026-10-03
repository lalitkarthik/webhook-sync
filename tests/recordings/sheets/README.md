# Google Sheets API v4 recordings (build step P3)

Recorded on 2026-10-03 between 04:46:53 and 04:53:29 UTC (10:16-10:23 IST), from a Claude Code cloud session on branch `feature/p3-sheets-sink`. They answer the live questions in [docs/research/sheets-live-facts.md](../../../docs/research/sheets-live-facts.md). P3's flusher tests and P5's FakeSheets replay them.

**Setup.** Calls went straight to `https://sheets.googleapis.com/v4/spreadsheets` with `httpx` 0.28.1. The bearer token came from `google-auth` 2.59.1 for the service account in `GOOGLE_SERVICE_ACCOUNT_B64`, with only the `https://www.googleapis.com/auth/spreadsheets` scope; recordings 31-32 are the exceptions and say so. The target was the owner's throwaway spreadsheet in `SHEET_ID` (title `webhook`, locale `en_GB`, time zone `Asia/Calcutta`). Its only tab, `Sheet1` (sheetId 0, 1000 x 26), was never written. The spike's own tabs were `wsync-spike-a` (sheetId 1600034012), `wsync-spike-b` (sheetId 1903783590) and `wsync-spike-c`, all deleted (42, 55, 62); 57 and 63 show only `Sheet1` left.

**Redaction.** The spreadsheet id reads `SHEET_ID`. The service-account address, project id, project number, client id and key id read `SA_EMAIL`, `PROJECT_ID`, `PROJECT_NUMBER`, `SA_CLIENT_ID` and `SA_KEY_ID`. The random id in 46-47 reads `RANDOM_ID`. No `Authorization` header is recorded. The `www-authenticate` header in 31 and 50 is Google's response header, not our token. In 15-17 the 50,000-character strings in the request are shortened to a placeholder (`body_note` says so). Response bodies are otherwise exactly as received.

## Format

Each `NN-<slug>.json` holds one HTTP call:

```json
{
  "what": "which check this is and what it asks",
  "ran_at": "UTC, to the second",
  "elapsed_ms": 0,
  "request": {"method": "...", "url": "...", "params": {}, "body": {}},
  "response": {"status": 0, "headers": {}, "body_bytes": 0, "body": {}}
}
```

`body_bytes` is the decoded body length. The wire was gzip, and `content-length` is absent because responses are chunked. `set-cookie` and `alt-svc` headers are dropped. `52-quota-burst-summary.json` is the only file that isn't a single call. It summarises 70 unrecorded reads and the recovery polls after them.

## Files

| # | Check | Call | Status | What it shows |
|---|---|---|---|---|
| 01 | 1 | `GET` `fields=sheets.properties` | 200 | Auth works; tabs with `sheetId`, `index`, `sheetType`, `gridProperties` (256 B) |
| 02 | 9 | `GET` `fields=spreadsheetId,properties` | 200 | `locale: en_GB`, `timeZone: Asia/Calcutta`, `autoRecalc`, default format, theme |
| 03 | 2 | `batchUpdate` `addSheet` 5 x 10 | 200 | Reply carries the new `sheetId` |
| 04 | 2 | `batchUpdate` `updateCells` by sheetId, typed | 200 | Header plus string, number (`0.00`), bool, date serial (DATE_TIME pattern), `=1+1`, `+919876543210`, `007`, a 19-digit number, `=HYPERLINK(…)` |
| 05 | 4 | `updateCells` at rowIndex 5 of a 5-row grid | 400 | `GridCoordinate.rowIndex[5] is after last row in grid[4]` |
| 06 | 2 | `appendDimension` 2 rows + `updateCells` at row 5, one call | 200 | Write lands in the new rows; `includeSpreadsheetInResponse` shape |
| 07 | 2 | `appendCells` by sheetId | 200 | Appends after the last row with data (row 7) |
| 08-10 | 2 | `values.get` by A1 title, UNFORMATTED / FORMATTED / FORMULA | 200 | Typed values back; `=1+1` stays text in all three; trailing empty cells and rows trimmed |
| 11 | 2 | `getByDataFilter` by sheetId, full cell mask | 200 | `userEnteredValue`, `effectiveValue`, `formattedValue`, `effectiveFormat.numberFormat` per cell |
| 12 | 4 | `GET` with no params | 200 | 3,559 B of metadata for two tabs |
| 13 | 4 | `GET` `ranges=<7x10>` `includeGridData=true`, no mask | 200 | 46,011 B |
| 14 | 4 | the same range with a field mask | 200 | 4,153 B |
| 15 | 4 | one 50,000-character cell | 200 | Accepted |
| 16 | 4 | one 50,001-character cell | 400 | `more than the maximum of 50000 characters in a single cell` |
| 17 | 4 | 49,999 `x` + one emoji (50,000 code points, 50,001 UTF-16 units) | 400 | The limit counts UTF-16 code units |
| 18 | 4 | `addSheet` with an empty 1,000,000 x 20 grid | 400 | `would increase the number of cells in the workbook above the limit of 20000000 cells`: empty cells count |
| 19 | 4 | `updateCells` then `appendDimension` +2,000,000 rows, one call | 400 | Same message, naming `requests[1].appendDimension` |
| 20 | 4 | `values.get` A3 after 19 | 200 | No `values` key: the `updateCells` before the failing request was not applied (atomic) |
| 21-22 | 6, 7 | `addSheet` B, then header and pay_A..pay_E with amounts 50, 10, 40, 20, 30 | 200 | |
| 23 | 6 | `createDeveloperMetadata` on the tab and on pay_B's row (index 2) | 200 | `metadataId`s, `locationType` SHEET and ROW |
| 24 | 6 | `developerMetadata:search` by key | 200 | Both found; row at index 2 |
| 25-27 | 3 | rename A, `updateCells` by sheetId, `getByDataFilter` by sheetId | 200 | The write lands; the read shows the new title |
| 28 | 3 | `values.get` with the old title | 400 | `Unable to parse range: 'wsync-spike-a'!A1:B2` |
| 29-30 | 6 | rename B, search metadata | 200 | Both metadata entries unchanged |
| 31 | 6 | Drive v3 `files.get` `fields=version,…` with the spreadsheets scope | 403 | `ACCESS_TOKEN_SCOPE_INSUFFICIENT` |
| 32 | 6 | the same with `drive.metadata.readonly` | 403 | `SERVICE_DISABLED`: the Drive API isn't enabled in the project |
| 33 | 7 | `getByDataFilter`, row 0 and column A by sheetId | 200 | Keys by row: pay_B at index 2 |
| 34 | 7 | `sortRange` rows 2-6 by Amount descending | 200 | A person's sort between read and write |
| 35 | 7 | `updateCells` at row index 2, no key cell | 200 | Succeeds silently |
| 36 | 7 | `getByDataFilter` key cell of row index 2 | 200 | `pay_C`, not `pay_B`: the post-write check sees the move |
| 37 | 7 | `values.get` whole tab | 200 | pay_C carries pay_B's update; pay_B is untouched on row 6 |
| 38 | 6 | search metadata after the sort | 200 | pay_B's row metadata moved from index 2 to 5 with its row |
| 39 | 6 | `values:batchUpdateByDataFilter` by metadata, RAW | 200 | Writes `A6:C6`, where pay_B now is |
| 40 | 6 | `values:batchGetByDataFilter` by metadata | 200 | Reads pay_B's row wherever it is |
| 41 | 5 | `updateCells` on sheetId 123456789 (never existed) | 400 | `Invalid requests[0].updateCells: No grid with id: 123456789` |
| 42 | 3 | `deleteSheet` A | 200 | |
| 43 | 3 | `updateCells` on the deleted sheetId | 400 | `Invalid requests[0].updateCells: No grid with id: 1600034012` |
| 44 | 3 | `getByDataFilter` on the deleted sheetId | 400 | `No sheet with id: 1600034012` |
| 45 | 3 | `appendDimension` on the deleted sheetId | 400 | `Invalid requests[0].appendDimension: No grid with id: …` |
| 46-47 | 5 | `GET` and `batchUpdate` on a random 44-character id | 404 | `Requested entity was not found.` |
| 48 | 5 | `GET` Google's public sample spreadsheet | 200 | Anyone-with-link view works without sharing |
| 49 | 5 | empty `batchUpdate` on the same sample | 403 | `The caller does not have permission` |
| 50 | 5 | `GET` with an invalid bearer token | 401 | `UNAUTHENTICATED` |
| 51 | 8 | read 68 of a quick burst | 429 | `RATE_LIMIT_EXCEEDED`, `ReadRequestsPerMinutePerUser` 60, `window_start_time`; no `Retry-After` |
| 52 | 8 | summary | | 66 reads in 9.7 s got 200; reads 67-70 got 429; a poll 10 s later got 200 |
| 53-54 | 9 | one date serial under five number formats | 200 | No format shows `46297.89513`; DATE_TIME without a pattern shows `02/10/2026 21:28:59` (en_GB); our pattern shows `2026-10-02 21:28:59` |
| 55 | cleanup | `deleteSheet` B | 200 | |
| 56 | 6 | search metadata after the delete | 200 | `{}`: the metadata went with the tab |
| 57 | cleanup | `GET` `fields=sheets.properties` | 200 | Only `Sheet1` is left |
| 58-59 | 2 | `addSheet` C (20 x 3), header plus pay_A..pay_C, a gap in pay_A's row | 200 | |
| 60 | 2 | `getByDataFilter`, row 0 and column A by sheetId | 200 | Trailing empty rows (5-20) and trailing empty cells are left out: the key block has 4 entries |
| 61 | 2 | `getByDataFilter` rows 1-2 | 200 | An empty cell between two filled ones comes back as `{}` |
| 62 | cleanup | `deleteSheet` C | 200 | |
| 63 | cleanup | `GET` `fields=sheets.properties` | 200 | Only `Sheet1` is left |
