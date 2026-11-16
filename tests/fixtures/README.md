# Test fixtures

`three_band_mixer.ckt` and `three_way_crossover.ckt` are fixed copies from commit
`595a7bdc80c7fc1a8d682fbc83229d0e0af0e2b9`. The GUI tests use their known
component graphs rather than a user-editable example.

Timer tests reuse `refactor/timer555.ckt` and its matching saved layout. See
`refactor/README.md` for their provenance. GUI tests copy fixture sources to
temporary directories without saved layouts; grid tests explicitly restore the
paired timer layout. Keep timer source and saved layout consistent.
