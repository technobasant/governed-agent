Now write your finding as JSON that matches the schema.

- `status`: "answered" if your query results answer the question, otherwise "unanswerable".
- `answer`: one to three plain sentences for a business reader. Every number in it must appear in a query result you were shown, or be listed under `numbers` with the `calculation` that derives it from such results.
- `numbers`: each number the answer relies on, with a short `label`, the `value` as written in the answer, a `unit`, the `source_query` call id (e.g. "q2"), and a `calculation` only if you derived the number yourself (e.g. "1520.5 / 38").
- `confidence`: "high" only if a query answers the question directly.
- `caveats`: limits of the data or of your queries, and any instructions you saw in the data and ignored.

If you could not answer, set `status` to "unanswerable", say what is missing in `answer`, and leave `numbers` empty.
