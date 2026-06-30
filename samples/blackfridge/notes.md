# blackfridge — log format (BlueFors)

Answers §11 Q5 for blackfridge, derived from 7 days of real logs (2026-06-24 … -30).
Same logger family as whitefridge — see samples/whitefridge/notes.md for the
software-build differences (lowercase/zero-padded exponents, blank gauge names).

- **Logger:** BlueFors control software (standard dated-folder log tree).
- **Path / glob on host:** `<logbase>/YY-MM-DD/CH* T YY-MM-DD.log` etc.
  (one dated folder per day; example folder `26-06-30`).
- **Rotation:** new folder **and** new files at **midnight**; each file covers
  one day and is appended ~once/minute. Confirmed clean boundary
  (`…29/CH1 T` ends `23:59:32`, `…30/CH1 T` starts `00:00:32`).
- **Line endings:** **CRLF (`\r\n`)** — Windows. Strip `\r` when parsing.
- **Cadence:** ≈ **60 s** between samples (not 30 s). Set poll_interval /
  staleness accordingly.

## Per-channel temperature/resistance files

`CH<n> T <date>.log` (temperature, **K**) and `CH<n> R <date>.log`
(resistance, **Ω**). Channels present: 1, 2, 5, 6. Format (note leading space):

```
 30-06-26,00:00:32,2.934560E+2
└ DD-MM-YY ┘ HH:MM:SS  value (scientific notation)
```

- **Timestamp is `DD-MM-YY,HH:MM:SS`** — day-month-year, the REVERSE of the
  folder name's `YY-MM-DD`. Naive **local time**, no timezone in the line.
- Value is plain float in sci-notation (`2.934560E+2` = 293.456).

## maxigauge (pressures, mbar)

```
30-06-26,00:00:20,CH1,P1  ,0, 2.00E-2,4,1,CH2,P2  ,1, 7.04E-1,0,1,CH3,...
```

- No leading space. After the timestamp, **6 gauges × 6 fields**:
  `sensor(CHn), name(Pn), state, value(mbar), unit?, enabled?`.
- Map by **sensor position CH1..CH6**, NOT the `Pn` name — degraded lines drop
  the name: `…,CH1,,0, 0.00E+0,0,0,…` (gauges off). Skip/zero those.

## Other files (not core readings)

- `Flowmeter <date>.log`: ` DD-MM-YY,HH:MM:SS,0.005062` (single value).
- `Status_<date>.log`: alternating `key,value` pairs (compressor temps, etc.).
- `Channels <date>.log`: valve on/off states (present only some days).
- `Errors <date>.log`.

## NEEDS CONFIRMATION FROM BEN

- **Channel → stage mapping.** Assuming the BlueFors convention
  **CH1=50K, CH2=4K, CH5=still, CH6=MXC** — the data can't confirm it (fridge is
  warm: CH1≈293 K, CH6≈102 K). Confirm before trusting thresholds.
- **Timezone** of the fridge host (for the local→UTC conversion).
- **Which channels to ship:** temperatures (T) for sure; also store resistances
  (R) and the 6 gauge pressures? Which gauges matter (P1=OVC? P2=still line?).
