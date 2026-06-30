# Sample log files

Drop representative raw log files from each fridge here — this is the critical
input that unblocks Phase 1 (the per-fridge parsers). See §11 Q5 in the spec and
[`docs/questions-for-ben.md`](../docs/questions-for-ben.md).

## Layout

One subfolder per fridge, named exactly as the fridge will be in config
(`fridge =` / the parser module name):

```
samples/
  bluefors_1/
    <copy of a real log file or two, verbatim>
    <ideally one from just after a midnight rotation, if files rotate>
    notes.md          # the metadata below for this fridge
  adr_2/
    ...
```

Start with the **ugliest** log format — that's the one to parse first.

## What to capture per fridge (`notes.md`)

- **Logger software / model**
- **Example log file path on the host** (e.g. `C:/BlueFors/logs/.../CH6 T.log`)
- **Filename pattern (glob)** and **how files rotate** (new file at midnight?
  append-forever? size-based?)
- **Timestamp format** (paste an example) and **timezone**
- **Units per column** (K? mK? mbar? Pa?)
- **Which column maps to which stage** (50K / 4K / still / MXC / GGG / FAA /
  pressures …)
- Any **edge-case lines** worth keeping: a partially-written final line, the
  first lines of a freshly-rotated file, a line with a missing/blank channel.

## Notes

- Paste files **verbatim** — don't reformat, trim, or "clean up" whitespace;
  the parser has to handle exactly what the logger writes.
- These are real-but-harmless instrument logs (temperatures/pressures), safe to
  commit. If any contain anything you'd rather not publish, tell me and we'll
  add an ignore rule or keep them out of git.
