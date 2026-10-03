# Research: live Google Sheets facts for the flusher (build step P3)

Researched 2026-10-03 against the live Sheets API v4 and Google's API reference, for the nine questions P3's flusher depends on. Every run was recorded. The recordings are in [tests/recordings/sheets/](../../tests/recordings/sheets/README.md) (63 files, one call each, plus a burst summary), so P3's tests and P5's FakeSheets can replay them. Recording numbers are written **#NN** below.

This note doesn't repeat what is already settled. G18-G25 are in [gaps-google-python-razorpay.md](gaps-google-python-razorpay.md) and [research-gaps.md](../design/research-gaps.md). The 2026-10-02 live checks of G18 (one `data` block per filter; a missing sheetId gives 400 "No sheet with id") and G21 (serials survive changes of locale and time zone) are in [razorpay-first.md](razorpay-first.md), "Sheets checks". Where a run here touches one of those, it cites it.

**How it ran.** The calls went from a Claude Code cloud session to `https://sheets.googleapis.com/v4/spreadsheets/…`, using `httpx` 0.28.1 and a token from `google-auth` 2.59.1 (both already in `pyproject.toml`). The token was for the service account in `GOOGLE_SERVICE_ACCOUNT_B64`, with only the `spreadsheets` scope. The key was decoded in memory only. The target was the owner's throwaway spreadsheet `SHEET_ID` (title `webhook`, locale `en_GB`, time zone `Asia/Calcutta`, one tab `Sheet1`). All work was in scratch tabs `wsync-spike-a`, `-b` and `-c`, and all three were deleted (#42, #55, #62). **The owner's tab was untouched.** Before the first call and after the last, a hash was taken of `Sheet1`'s properties, every cell's `userEnteredValue` and `userEnteredFormat`, its developer metadata, protected ranges and filter views, plus the spreadsheet's named ranges and metadata. The two hashes are identical. #57 and #63 show `Sheet1` as the only tab. The spreadsheet's locale and time zone were never changed. No secret, spreadsheet id or service-account address is in this note or the recordings: they read `SHEET_ID`, `SA_EMAIL`, `PROJECT_ID` and `PROJECT_NUMBER`.

| # | Question | Verdict | One-line finding |
|---|---|---|---|
| 1 | Auth and tab list | **confirmed** | `GET ?fields=sheets.properties` gives every tab's `sheetId`, `title`, `index`, `sheetType` and `gridProperties` in 256 B (#01). |
| 2 | Typed writes by sheetId; reading back | **confirmed, with shapes** | One `batchUpdate` of `updateCells` by sheetId stores each type as given. `=1+1`, `=HYPERLINK(…)`, `+91…` and `007` stay text (G20 now confirmed live). `appendDimension` and `updateCells` in one call land in the new rows. Reads leave out trailing empty rows and cells, and an interior gap is `{}`. |
| 3 | Rename, then delete | **confirmed** | After a rename, a write by sheetId lands and a read by sheetId follows. A read by the old A1 title gives 400 "Unable to parse range". After a delete: **400** "No grid with id: N" (`batchUpdate`) or "No sheet with id: N" (`getByDataFilter`). **Never 404.** |
| 4 | Limits and sizes | **settled by runs** | 20,000,000 cells, **empty grid cells included**, enforced on `addSheet` and `appendDimension`. 50,000 per cell, **counted in UTF-16 code units**. Writing past `rowCount` gives 400 "rowIndex[5] is after last row in grid[4]". A field mask cuts a 7 x 10 grid read from 46,011 B to 4,153 B. |
| 5 | Bad sheetId, no access | **settled by runs** | Bad sheetId: 400 `INVALID_ARGUMENT`. Unknown spreadsheet: **404** "Requested entity was not found." View-only spreadsheet, on write: **403** "The caller does not have permission". Bad token: 401. |
| 6 | Detecting "changed during write" | **no cheap compare-and-set exists** | Sheets has no etag, revision or If-Match. Drive's `version` needs a Drive scope and the Drive API enabled (both refused, #31-#32). Developer metadata survives a rename. **Row metadata moves with its row through a sort**, and a write can be addressed by it, but only through the values API, and there's a 30,000-character budget per sheet. |
| 7 | Sort between read and write (F05) | **reproduced** | After the sort, a write by row number put pay_B's update onto pay_C's row and succeeded silently. The design's post-write re-read of the key cell saw `pay_C` and caught it. |
| 8 | Quota | **settled by runs** | 429 `RESOURCE_EXHAUSTED` / `RATE_LIMIT_EXCEEDED`, `ReadRequestsPerMinutePerUser` = 60. **No `Retry-After` header.** The body has `window_start_time`. 66 reads passed before the first 429, and reads worked again about 10 s later, once the window rolled over. |
| 9 | Time zone and date display | **confirmed** | `locale: en_GB`, `timeZone: Asia/Calcutta`. A serial with our pattern shows `2026-10-02 21:28:59`. Without a pattern it follows the locale: `02/10/2026 21:28:59` for DATE_TIME, `46297.89513` with no format. |

## 1. Auth and the tab list

**Run.** `GET /v4/spreadsheets/SHEET_ID?fields=sheets.properties` returned 200 (#01). Each tab is `{"properties": {"sheetId", "title", "index", "sheetType": "GRID", "gridProperties": {"rowCount", "columnCount"}}}`. `Sheet1` has sheetId `0`. Tabs created later get large random ids (1600034012, 1903783590). `fields=spreadsheetId,properties` (#02) gives `title`, `locale`, `autoRecalc`, `timeZone`, `defaultFormat` and `spreadsheetTheme`.

## 2. Typed writes by sheetId, then reading back

**Runs.** `addSheet` with a 5 x 10 grid. Its reply carries `replies[0].addSheet.properties.sheetId` (#03). Then one `batchUpdate` with one `updateCells` (`start: {sheetId, rowIndex: 0, columnIndex: 0}`, `fields: "userEnteredValue,userEnteredFormat.numberFormat"`) (#04). Every successful `batchUpdate` reply in this run was `{"spreadsheetId", "replies": [{}…], "commentUpdateState": "NO_UPDATES_REQUESTED"}`, with one empty reply per request. `commentUpdateState` appears in the reference's response body ([batchUpdate](https://developers.google.com/workspace/sheets/api/reference/rest/v4/spreadsheets/batchUpdate)). FakeSheets should send it.

| Written as | `effectiveValue` | FORMATTED_VALUE | Note |
|---|---|---|---|
| `stringValue "pay_A"` | `stringValue` | `pay_A` | |
| `numberValue 500.5`, pattern `0.00` | `numberValue 500.5` | `500.50` | |
| `boolValue true` | `boolValue true` | `TRUE` | |
| `numberValue 46297.895127314812`, DATE_TIME `yyyy-mm-dd hh:mm:ss` | the same number | `2026-10-02 21:28:59` | `(unix + 19800)/86400 + 25569` for 2026-10-02 15:58:59 UTC |
| `stringValue "=1+1"` | `stringValue "=1+1"` | `=1+1` | **Stays text**; the FORMULA render also returns the string (#10) |
| `stringValue '=HYPERLINK("https://example.com","x")'` | `stringValue` | the same text | Stays text |
| `stringValue "+919876543210"`, `"007"` | `stringValue` | unchanged | |
| `numberValue 1234567890123456789` | `1.2345678901234568e+18` | `1.23457E+18` | A double; the design already writes past 15 digits as text |

This confirms G20 live, end to end. The reference says `stringValue` "Represents a string value" and dates are "doubles in SERIAL_NUMBER format" ([ExtendedValue](https://developers.google.com/workspace/sheets/api/reference/rest/v4/spreadsheets/other#ExtendedValue)).

**Writing after the last row.** These two shapes work:
- `appendDimension` (ROWS, 2) followed by `updateCells` at `rowIndex 5` in the **same** `batchUpdate` succeeds. Requests apply in order, so the write lands in the rows just added (#06). This is the design's F07 path.
- `appendCells` by sheetId "Adds new cells after the last row with data in a sheet, inserting new rows into the sheet if necessary" ([Request](https://developers.google.com/workspace/sheets/api/reference/rest/v4/spreadsheets/request)). It landed on row 7, after pay_F (#07). Its reply is `{}` and doesn't say which row it used, so the design's "re-read the keys of the rows written" check would have to search for them. Keep `appendDimension` + `updateCells`.

**Reading back.**
- `values.get` by A1 with the tab's title, `'wsync-spike-a'!A1:J10` (#08-#10). The returned `range` is **clipped to the grid** (`A1:J7`), although the reference says it "indicates the entire requested range". Empty rows in the middle come back as `[]`, and trailing empty rows and columns are left out, as the reference says ("For output, empty trailing rows and columns will not be included", [ValueRange](https://developers.google.com/workspace/sheets/api/reference/rest/v4/spreadsheets.values)). When nothing is there, the `values` key is missing entirely (#20). The default render is FORMATTED_VALUE, and dates default to SERIAL_NUMBER ([values.get](https://developers.google.com/workspace/sheets/api/reference/rest/v4/spreadsheets.values/get)).
- By sheetId: `spreadsheets.get` takes only A1 `ranges`, which means titles. **`getByDataFilter` is the way to read by sheetId** (#11, #27, #33, #60). Shapes FakeSheets must copy:
  - `startRow` and `startColumn` are left out when they are 0 (#11, #60).
  - Rows past the grid are clipped: 0-10 asked of a 7-row grid gave 7 `rowData` entries.
  - **Trailing empty rows and trailing empty cells are left out**: a 20-row grid with 4 used rows gave 4 key entries (#60).
  - An empty row in the middle is `{}` (#11), and an empty cell between two filled ones is `{}` (#61).
  - `effectiveFormat.numberFormat` comes back only where a format was set.

  The design's "last key row = last used row" holds: the key block's length is the used height.

## 3. Rename, then delete

- `updateSheetProperties` to rename (#25). `updateCells` by sheetId then landed (#26), and `getByDataFilter` by sheetId read it back under the new title (#27). An A1 read with the old title gives **400** `INVALID_ARGUMENT`, "Unable to parse range: 'wsync-spike-a'!A1:B2" (#28). This confirms F08's premise.
- After `deleteSheet` (#42):

| Call on the deleted sheetId | Status | `error.message` |
|---|---|---|
| `batchUpdate` `updateCells` (#43) | 400 `INVALID_ARGUMENT` | `Invalid requests[0].updateCells: No grid with id: 1600034012` |
| `getByDataFilter` (#44) | 400 `INVALID_ARGUMENT` | `No sheet with id: 1600034012` (as razorpay-first.md recorded) |
| `batchUpdate` `appendDimension` (#45) | 400 `INVALID_ARGUMENT` | `Invalid requests[0].appendDimension: No grid with id: 1600034012` |

None of these is a 404. No error body has a `details` array, so the class has to come from the message text.

## 4. Limits, sizes, and writing past the grid

- **Past the grid.** `updateCells` at rowIndex 5 on a 5-row tab gives 400, "Invalid requests[0].updateCells: GridCoordinate.rowIndex[5] is after last row in grid[4]" (#05). The flusher sends this when its read is stale: a person deleted rows between our read and our write, or the grid wasn't extended.
- **Cell limit.** `addSheet` with an empty 1,000,000 x 20 grid, on top of 26,070 existing cells, gives 400, "Invalid requests[0].addSheet: This action would increase the number of cells in the workbook above the limit of 20000000 cells." (#18). **Empty grid cells count**, and the limit on this spreadsheet is 20,000,000. That settles G22's open spike and matches the doubled limit ([Drive help](https://support.google.com/drive/answer/37603); [Workspace Updates, Sept 2026](https://workspaceupdates.googleblog.com/2026/09/doubled-cell-limits-in-google-sheets-now-generally-available.html)). The same text comes from `appendDimension` as `requests[1].appendDimension` (#19). The `updateCells` placed before it in the same call was **not** applied (#20). That is the documented atomicity: "If any request is not valid then the entire request will fail and nothing will be applied" ([batchUpdate](https://developers.google.com/workspace/sheets/api/reference/rest/v4/spreadsheets/batchUpdate)).
- **Per-cell limit.** 50,000 × `x` is accepted (#15). 50,001 gives 400, "Invalid requests[0].updateCells: Your input contains more than the maximum of 50000 characters in a single cell." (#16). 49,999 × `x` plus one emoji is 50,000 code points but 50,001 UTF-16 units, and it is **rejected** (#17). The limit counts UTF-16 code units, so Python's `len()` is the wrong measure.
- **Error index.** Every 400 from `batchUpdate` names the failing request: `Invalid requests[N].<kind>: …`. Only the first failing request is reported.
- **Response sizes**, decoded:

| Call | Bytes |
|---|---|
| `GET ?fields=sheets.properties` (#01) | 256 |
| `GET`, no params, two tabs, no grid data (#12) | 3,559 |
| `GET ?ranges=<7x10>&includeGridData=true`, no mask (#13) | 46,011 |
| The same with mask `sheets(properties(sheetId,gridProperties),data(rowData(values(effectiveValue))))` (#14) | 4,153 |
| `getByDataFilter`, all four value fields plus `numberFormat`, 7 x 10 (#11) | 8,553 |

  Without a mask, grid data is about 650 B a cell. With `effectiveValue` only, it's about 60 B. The flusher must always send a mask. G18 notes that `includeGridData` is ignored once a mask asks for `sheets.data`.

## 5. Bad sheetId and no access

| Case | Status | Body |
|---|---|---|
| `updateCells` on a sheetId that never existed (#41) | 400 `INVALID_ARGUMENT` | `Invalid requests[0].updateCells: No grid with id: 123456789` |
| `GET` / `batchUpdate` on a random, well-formed 44-character id (#46, #47) | **404** `NOT_FOUND` | `Requested entity was not found.` |
| `GET` on Google's public quickstart sample (anyone with the link can view; not shared with us) (#48) | 200 | Readable |
| empty `batchUpdate` (`requests: []`) on the same sample (#49) | **403** `PERMISSION_DENIED` | `The caller does not have permission` |
| invalid bearer token (#50) | 401 `UNAUTHENTICATED` | `Request had invalid authentication credentials…`, with `www-authenticate: Bearer … error="invalid_token"` |

**Not tested:** an existing *private* spreadsheet that isn't shared with the account. Sheets may answer 403 or 404 there. The owner can settle it by unsharing a throwaway sheet from the service account. Until then, the flusher must treat 404 on the spreadsheet as "spreadsheet missing or unshared", not "tab missing".

## 6. Detecting "sheet changed during write"

**Sheets has no optimistic-concurrency hook.** No response carried an `ETag` header (see the headers in any recording), and no Spreadsheet field is a revision. `batchUpdate` takes no precondition. Its reference only warns: "Your changes may be altered with respect to collaborator changes" ([batchUpdate](https://developers.google.com/workspace/sheets/api/reference/rest/v4/spreadsheets/batchUpdate)).

**Drive's file `version`.** It isn't available under the design's auth. With the `spreadsheets` scope only, `files.get` gives 403 `ACCESS_TOKEN_SCOPE_INSUFFICIENT` (#31). With `drive.metadata.readonly`, it gives 403 `SERVICE_DISABLED`, because the Drive API isn't enabled in the project (#32). Even enabled, `version` changes on our own writes too, so it can't tell a person's sort from our own flush.

**Developer metadata** ([guide](https://developers.google.com/workspace/sheets/api/guides/metadata), [resource](https://developers.google.com/workspace/sheets/api/reference/rest/v4/spreadsheets.developerMetadata)):
- `createDeveloperMetadata` on the tab (`location: {sheetId}`) and on pay_B's row (`location: {dimensionRange: {sheetId, ROWS, 2, 3}}`), both `visibility: DOCUMENT` (#23). Searching by key returns `matchedDeveloperMetadata[]`, each with its `location` (#24).
- **Rename.** Both entries came through unchanged (#30).
- **Sort.** The row entry moved with its row: `startIndex` 2 became 5 when pay_B sorted to the bottom (#38). The reference documents this for inserts, and the run shows it for `sortRange`. The reference also says deleting an object deletes its metadata: after `deleteSheet`, the search returns `{}`, with no `matchedDeveloperMetadata` key (#56).
- **Writing through it.** `values:batchUpdateByDataFilter` with a `developerMetadataLookup` (key `wsync.payment`, value `pay_B`) wrote `A6:C6`, where pay_B was after the sort (#39). `values:batchGetByDataFilter` read it back the same way (#40). `RAW` kept `=1+1 via metadata` as text.
- **Why it isn't a fix.**
  - It works only through the **values** API: whole row ranges from column A, with RAW parsing, not typed `CellData` with a number format. `spreadsheets.batchUpdate`'s `updateCells` takes a GridRange, not a DataFilter.
  - Metadata storage is capped at "30,000 characters" per sheet and per spreadsheet, keys and values included (guide). That is a few hundred payment rows.
  - Tab-level metadata **is** a cheap, rename-proof place to keep a marker such as the deployment id. It goes when the tab goes.

**Conclusion.** The design's post-write key check (step 9) is the cheapest working detector. Section 7 shows it works.

## 7. A sort between read and write (F05)

Tab B held pay_A..pay_E with amounts 50, 10, 40, 20, 30.
1. Read: two `getByDataFilter` filters by sheetId (row 0, column A) gave keys by row `[Payment ID, pay_A, pay_B, pay_C, pay_D, pay_E]`, so pay_B was at index 2 (#33).
2. A person's `sortRange` sorted rows 2-6 by Amount, descending (#34).
3. The flusher wrote pay_B's update by the row number it had read: `updateCells` at rowIndex 2, columns B-C, with **no key cell**. The result was 200, with no error (#35).
4. The post-write check re-read the key cell of row index 2 and got **`pay_C`** (#36).

The tab afterwards (#37):

| Row | Payment ID | Amount | Note |
|---|---|---|---|
| 2 | pay_A | 50 | |
| 3 | **pay_C** | **11** | **update for pay_B** |
| 4 | pay_E | 30 | |
| 5 | pay_D | 20 | |
| 6 | pay_B | 10 | |

So F05 is real. Because the update leaves the key cell alone, the corruption shows up as a key mismatch on the next read. That confirms sheets-sink.md step 9 and ADR 0006's "the post-write check notices a moved row and repairs it". The repair must rewrite **pay_C** as well as pay_B: pay_C's Amount and Note now hold pay_B's values. The design's "every key involved gets a mirror job" already covers that.

## 8. Quota

**Documented** ([limits](https://developers.google.com/workspace/sheets/api/limits)). 300 reads and 300 writes a minute per project, and 60 of each per minute per user per project. Exceeding them returns "429: Too many requests". Google recommends truncated exponential backoff, `min((2^n) + random_ms, maximum_backoff)`, with a maximum of 32 or 64 s. It recommends keeping a payload under 2 MB, and a request running past 180 s times out. "Exceeding the quota request limits is planned to incur charges to your Google Cloud billing account later in 2026." The page doesn't mention `Retry-After`.

**Run** (#51, #52). There were 70 quick `GET ?fields=spreadsheetId` reads, about 7 a second. Reads 1-66 got 200, and reads 67-70 got **429**. #51's body:

```json
{"error": {"code": 429, "status": "RESOURCE_EXHAUSTED",
  "message": "Quota exceeded for quota metric 'Read requests' and limit 'Read requests per minute per user' of service 'sheets.googleapis.com' for consumer 'project_number:PROJECT_NUMBER'.",
  "details": [{"@type": "type.googleapis.com/google.rpc.ErrorInfo", "reason": "RATE_LIMIT_EXCEEDED", "domain": "googleapis.com",
    "metadata": {"quota_metric": "sheets.googleapis.com/read_requests", "quota_limit": "ReadRequestsPerMinutePerUser",
      "quota_limit_value": "60", "quota_unit": "1/min/{project}/{user}", "window_start_time": "1791002993", …}}, …]}}
```

- There is **no `Retry-After` header** (#51's headers).
- `window_start_time` was 04:49:53 UTC, and the burst ran 04:50:42-04:50:52. A poll at 04:51:02, just after the window ended, got 200. The quota looks like a per-minute window that the error names, so `window_start_time + 60 − now` is a usable wait.
- Enforcement is approximate: at least 66 reads passed in one window against a limit of 60. Don't build on the exact count.

The design's 40 reads and 20 writes a minute at worst stays inside the limit.

## 9. Time zone and date display

`properties.locale` is `en_GB` and `properties.timeZone` is `Asia/Calcutta`, the legacy IANA alias of `Asia/Kolkata` (#02). One serial (2026-10-02 21:28:59 IST) under five formats, read back by sheetId (#53, #54):

| `numberFormat` | `formattedValue` |
|---|---|
| none | `46297.89513` |
| `{type: DATE_TIME}`, no pattern | `02/10/2026 21:28:59` (en_GB order) |
| `{type: DATE}`, no pattern | `02/10/2026` |
| `{type: DATE_TIME, pattern: "yyyy-mm-dd hh:mm:ss"}` | `2026-10-02 21:28:59` |
| `{type: DATE_TIME, pattern: "dd/mm/yyyy hh:mm"}` | `02/10/2026 21:28` |

The spreadsheet time zone isn't applied to a stored serial: the serial is shown as written. That matches the G21 run (razorpay-first.md, five locale and time-zone pairs). A format without a pattern follows the locale, so the writer must always send the explicit pattern. The [formats guide](https://developers.google.com/workspace/sheets/api/guides/formats) describes the locale default.

## What the design should change

In [sheets-sink.md](../design/sheets-sink.md):

1. **Failures table, row "404, or a 400 naming a sheet id, grid or range".**
   - A deleted tab never gives 404. It gives 400 "No grid with id" (writes) or "No sheet with id" (`getByDataFilter`).
   - A 404 means the spreadsheet itself can't be found: deleted, or a wrong id, and possibly unshared (untested). Give 404 its own reason, "spreadsheet missing".
   - Keep "tab missing" for the two 400 texts above.
   - "is after last row in grid" (#05) means the read was stale, for example a person deleted rows. It should be `retry`: the next flush re-reads the grid. It isn't a pause.
2. **Failures table, "A 400 naming one request's cell value: split the batch in halves".** Every 400 names the failing request as `requests[N]`, so there's no need to bisect. Map N to its job, mark that job `dead`, and resend the rest. Only the first bad request is reported per call, so a batch with several bad rows needs one round per bad row. That is still fewer than halving.
3. **Failures table, 429 "honouring `Retry-After`".** Sheets sends no `Retry-After` on 429 (#51). Use Google's truncated exponential backoff, or wait until `window_start_time + 60 s` from the error details. Honour `Retry-After` only if a 5xx ever carries one.
4. **Encoding table, "Cells over 50,000 characters are cut".** Measure in **UTF-16 code units** (`len(s.encode("utf-16-le")) // 2`), with the `…[truncated]` suffix counted inside the 50,000 and the cut placed so it doesn't split a surrogate pair (#17).
5. **Classifying errors.** Every 400 is `INVALID_ARGUMENT` with no `details`, so the class has to come from matching message text: "No grid with id", "No sheet with id", "above the limit of 20000000 cells", "is after last row in grid", "more than the maximum of 50000 characters". Keep the matched strings in one table with the recordings as fixtures, so a wording change fails a test rather than a buyer's flush.
6. **"Sheet full".** Confirmed: the limit is 20,000,000 and empty grid cells count (#18). The design's default and its "API's own cell-limit error" rule hold. The error arrives on `appendDimension`, the step that grows the grid, and nothing in that batch is applied (#19, #20).

Things that **hold** as designed: reads and writes by sheetId follow a rename (F08); `appendDimension` and `updateCells` work in one atomic call (F07); typed `stringValue` keeps formula-like text as text (Q23, G20); a field mask is essential; the post-write key check catches F05, and pay_C needs repair as well as pay_B; the explicit date pattern displays the same in any locale; the 60-per-minute quota; no cheap revision or etag exists, so step 9 stays.

## API runs

All on 2026-10-03, 04:46:53-04:53:29 UTC, against `sheets.googleapis.com` (plus two Drive calls), with the service account's `spreadsheets`-scope token unless noted. Full requests and responses are in [tests/recordings/sheets/](../../tests/recordings/sheets/README.md).

| # | Call | Status | Shape seen |
|---|---|---|---|
| 01 | `GET ?fields=sheets.properties` | 200 | One tab, `Sheet1`, sheetId 0, 1000 x 26 |
| 02 | `GET ?fields=spreadsheetId,properties` | 200 | `en_GB`, `Asia/Calcutta`, `ON_CHANGE` |
| 03 | `batchUpdate addSheet` | 200 | `replies[0].addSheet.properties.sheetId` |
| 04 | `batchUpdate updateCells` by sheetId, typed | 200 | `replies: [{}]`, `commentUpdateState` |
| 05 | `updateCells` past `rowCount` | 400 | `GridCoordinate.rowIndex[5] is after last row in grid[4]` |
| 06 | `appendDimension` + `updateCells`, one call | 200 | Two empty replies; `updatedSpreadsheet` when asked |
| 07 | `appendCells` | 200 | `{}`; landed after the last row with data |
| 08-10 | `values.get` A1 by title, three render options | 200 | Text stays text; `range` clipped to grid; `[]` for empty middle rows |
| 11 | `getByDataFilter` by sheetId, full mask | 200 | `startRow` left out when 0; `{}` for empty rows |
| 12-14 | `GET` with and without a mask | 200 | 3,559 / 46,011 / 4,153 B |
| 15-17 | 50,000 / 50,001 / 50,000-plus-emoji characters | 200 / 400 / 400 | UTF-16 units counted |
| 18-19 | `addSheet` / `appendDimension` past 20 M cells | 400 | `above the limit of 20000000 cells` |
| 20 | `values.get` after the failed batch | 200 | No `values`: nothing applied |
| 21-24 | Tab B, rows, developer metadata, search | 200 | SHEET and ROW locations |
| 25-28 | Rename A; write and read by sheetId; read by the old title | 200 / 200 / 200 / 400 | `Unable to parse range` |
| 29-30 | Rename B; metadata search | 200 | Metadata unchanged |
| 31-32 | Drive `files.get` version | 403 / 403 | Scope insufficient; Drive API disabled |
| 33-37 | F05: read, sort, write by row number, check, whole tab | 200 | Update on pay_C's row; check sees `pay_C` |
| 38 | Metadata search after the sort | 200 | Row metadata moved 2 → 5 |
| 39-40 | values write and read by metadata | 200 | `A6:C6` |
| 41 | `updateCells` on a sheetId that never existed | 400 | `No grid with id` |
| 42-45 | Delete A; write, read, `appendDimension` on it | 200 / 400 / 400 / 400 | `No grid with id` / `No sheet with id` |
| 46-47 | Random spreadsheet id | 404 | `Requested entity was not found.` |
| 48-49 | Google's public sample: read; empty write | 200 / 403 | `The caller does not have permission` |
| 50 | Invalid token | 401 | `UNAUTHENTICATED` |
| 51-52 | 70-read burst | 200 ×66, then 429 | `RATE_LIMIT_EXCEEDED`, `window_start_time`, no `Retry-After`; 200 again 10 s later |
| 53-54 | Date serial under five formats | 200 | Explicit pattern locale-proof; default follows `en_GB` |
| 55-57 | Delete B; metadata search; tab list | 200 | `{}`; only `Sheet1` |
| 58-61 | Tab C: rows, key read, gap read | 200 | Trailing empties left out; interior gap `{}` |
| 62-63 | Delete C; tab list | 200 | Only `Sheet1`; `Sheet1`'s content hash unchanged |

## Sources

- Sheets API usage limits: https://developers.google.com/workspace/sheets/api/limits
- `spreadsheets.batchUpdate` (atomicity, ordering, `commentUpdateState`, collaborator note): https://developers.google.com/workspace/sheets/api/reference/rest/v4/spreadsheets/batchUpdate
- Request types (`AppendCellsRequest`, `AppendDimensionRequest`, `UpdateCellsRequest`, `SortRangeRequest`, `DeleteSheetRequest`): https://developers.google.com/workspace/sheets/api/reference/rest/v4/spreadsheets/request
- `ExtendedValue`: https://developers.google.com/workspace/sheets/api/reference/rest/v4/spreadsheets/other#ExtendedValue
- `spreadsheets.getByDataFilter`: https://developers.google.com/workspace/sheets/api/reference/rest/v4/spreadsheets/getByDataFilter
- `ValueRange` (trailing rows and columns left out): https://developers.google.com/workspace/sheets/api/reference/rest/v4/spreadsheets.values
- `spreadsheets.values.get` (render defaults): https://developers.google.com/workspace/sheets/api/reference/rest/v4/spreadsheets.values/get
- Developer metadata guide (locations, 30,000-character limits, visibility): https://developers.google.com/workspace/sheets/api/guides/metadata
- `spreadsheets.developerMetadata` (location types, metadata follows its row, removed with its object): https://developers.google.com/workspace/sheets/api/reference/rest/v4/spreadsheets.developerMetadata
- Number formats: https://developers.google.com/workspace/sheets/api/guides/formats
- Cell limit: https://support.google.com/drive/answer/37603 and https://workspaceupdates.googleblog.com/2026/09/doubled-cell-limits-in-google-sheets-now-generally-available.html (both via [gaps-google-python-razorpay.md](gaps-google-python-razorpay.md) G22)
- Live runs: [tests/recordings/sheets/](../../tests/recordings/sheets/README.md)
