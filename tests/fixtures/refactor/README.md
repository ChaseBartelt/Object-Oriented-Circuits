# Pre-refactor regression fixtures

These are a small reference set from the November 15, 2026 architecture audit on
`For-IDEs`, based on commit `78cdeccb0ffeade251450f55349f5f05da0bcd68` plus the
working changes recorded at that time. They are owned test data, independent of
editable examples.

- Source `.ckt` files and SPICE `.cir` outputs are copied from the audit snapshot
  and its executed reference capture.
- `expectations.json` preserves exact electrical graphs, names, values, terminal
  mapping, hierarchy ownership, source lines and normalized source origins.
- Numerical results use `rtol=1e-9` and `atol=1e-11`, matching the captured MATLAB
  parity suite's numerical tolerance. These are behavior-preservation checks,
  not independent claims of semiconductor model accuracy.
- The divider deliberately has the audited 10 kohm lower resistor and therefore
  a 10 V output. Its original introductory comment describes the earlier 4 V
  demonstration; that comment is preserved to keep the audited source intact.
  The separate parent fixture `../voltage_divider.ckt` owns the 4 V CLI/GUI test.
- The packaged NAND uses the current `@chips` library. Changes to those library
  definitions must be reviewed against the preserved graph and solver outputs.
- `timer555.ckt.layout.json` is the original saved v1 layout. Its expected restored
  root and expanded-child geometry includes repairs already performed by the
  pre-refactor reader. It is not compared naïvely to the raw legacy JSON.

Do not regenerate expected results from the current implementation just to make
a test pass. Investigate differences and document intentional behavior changes.
Source hashes and provenance are recorded in `expectations.json`.
