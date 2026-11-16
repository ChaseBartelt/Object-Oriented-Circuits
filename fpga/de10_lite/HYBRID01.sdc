# Board oscillator: 50 MHz, 20 ns period.
create_clock -name MAX10_CLK1_50 -period 20.000 [get_ports {MAX10_CLK1_50}]
derive_clock_uncertainty

# SW0 is asynchronous. Cut only the pad-to-first-stage path.
# The sw_meta -> sw_sync path remains timed at 50 MHz.
set first_stage [get_registers {sw_meta}]
if {[get_collection_size $first_stage] != 1} {
    error "HYBRID01 requires exactly one sw_meta synchronizer register"
}
set_false_path -from [get_ports {SW0}] -to $first_stage

# Human-visible LED and passive piezo have no external receiving clock.
# There is no synchronous output-delay requirement for these loads.
set_false_path -to [get_ports {LEDR0 AUDIO_OUT}]
