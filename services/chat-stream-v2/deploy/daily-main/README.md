# Daily main gate

Render the sibling plist's home and calendar placeholders in local deployment
configuration after the resource-window owner assigns a nonoverlapping slot.
Install the launcher and plist using the existing user-job procedure. The local
`~/.pentacle/daily-main/config.zsh` supplies `CHECKOUT` (the service checkout,
including its interpreter) and `REPOSITORY` (the public repository URL). The
launcher uses the existing nightly-journey window and fire guard.

Each attempt appends one line to `~/.pentacle/daily-main/results.log`. A gate
launch records its detached main SHA, self-comparison, profile, outcome and
unique evidence directory. A checkout or setup refusal records
`NOT_LAUNCHED` and its reason. The supervisor's 11,400-second bound includes checkout,
10,800 seconds for the gate and early shutdown; the window also needs the
scheduler's separate 600-second reserve. After checkout the launcher requires the full gate budget plus five seconds
to start it before the supervisor sends its termination signal.

The launcher checks the 60 GiB free-space floor at admission. A repeated-run
controller must also stop before another run would cross that floor. Keep all
first-attempt evidence for the required series; neither this launcher nor the
existing deployed-release nightly job prunes that series.
