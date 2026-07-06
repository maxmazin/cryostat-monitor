# whitefridge — log format (BlueFors)

Answers §11 Q5 for whitefridge, derived from real logs (2026-06-23 … -30).

**Same logger family as blackfridge** (see samples/blackfridge/notes.md for the
shared structure: dated daily folders, `CH<n> T/R` files, `maxigauge`, midnight
rotation, CRLF, day-month-year timestamps). This host runs a **different BlueFors
software build**; the differences below are the only things that matter, and the
parser already tolerates all of them.

## Differences from blackfridge

- **Numeric format:** lowercase, zero-padded exponent — `2.793549e+01` (vs
  blackfridge `2.793549E+1`). Still scientific notation; `float()` parses both.
- **No leading space** before the timestamp on `CH<n>` lines (blackfridge has one).
- **maxigauge gauge names are BLANK on every line**, even when the gauge is live:
  ```
  30-06-26,00:00:02,CH1,        ,0,2.00e-02,4,1,CH2,        ,1,5.83e-02,0,1,...
  ```
  Map gauges by **sensor position CH1..CH6** and decide on/off from the **state
  field** (the 3rd field of each gauge group, 0/1), NOT the name — otherwise
  every pressure is silently dropped. In the line above CH1 is OFF: state 0,
  value frozen at the placeholder 2.00e-02, Pfeiffer status code 4 (= sensor
  off) in the 5th field. The trailing 6th field stays 1 even for off gauges,
  so it is NOT an enable flag.
- **Cadence ≈ 10 s** (blackfridge ≈ 60 s). The watchdog `poll_interval` in
  fridges.yaml is the daemon's *reporting* cadence, not this log cadence.
- **Extra `Heaters <date>.log`** file (` DD-MM-YY,HH:MM:SS,<n>,<float>,<n>,<float>`).
  Not a core reading source; not shipped.

## Channels present

`CH1, CH2, CH5, CH6` (T and R), same as blackfridge. Rotation confirmed clean at
midnight (`…29/CH6 T` ends `23:59:52`, `…30/CH6 T` starts `00:00:02`).

## NEEDS CONFIRMATION FROM BEN (same as blackfridge)

- **Channel → stage mapping.** Assuming the BlueFors convention
  **CH1=50K, CH2=4K, CH5=still, CH6=MXC**; the fridge is warm in the samples
  (CH1/2/5 ≈ 293 K, CH6 ≈ 28 K) so the data can't confirm it.
- **Timezone** of the fridge host (for local→UTC conversion).
- **Which channels to ship** beyond temperatures (resistances? which gauges?).
