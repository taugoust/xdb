# Source coverage workflow

Before drawing a conclusion, pin the exact product, mode/configuration, and version or release. Start at its primary guide, then follow that guide's exact known-issues index and relevant child advisories. Do not substitute a similarly named guide's index: for example PG347/AR75396 and PG346/AR75350 are a mismatch warning, not interchangeable references. Also inspect applicable manuals/register references, errata, and vendor examples.

Record a coverage table with source, exact revision/date, applicable scope, topics checked, relevant evidence (including topic/answer-record IDs), and gaps. State a stopping point based on the sources actually checked; a public document or successful search does not establish that all sources were searched. A page/API error is evidence only for that route and status, not proof that documentation is inaccessible. Do not infer IDs from pretty URLs.

Use `xdb docs maps` and `xdb docs search` to identify candidates, verify Document_ID/product/version, then read the exact topic. `xdb docs refs MAP_ID TOPIC_ID` lists links embedded in its topic with source/revision provenance without fetching destinations. Public-document versus external-support labels indicate hostname only, not accessibility or authentication status.
