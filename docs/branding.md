# Pentacle branding

Web icons use the mobile app's actual launcher and adaptive derivatives from
[the public mobile repository](https://github.com/HJK6/pentacle-mobile/tree/cfccc24c6b3a7315f43c24aa9100ce7287909325/assets/images).
`renderer/assets/mobile-icon-sources.json` pins their commit, paths and SHA256s.
The two source PNGs are byte-exact copies: the full canvas, background, spacing
and artwork are preserved. Mobile's launcher already centers the original
artwork by translating it 2 pixels right and 29 pixels down at 1024 pixels.
The unchanged original `pentacle-logo-source.png` is archival, not the resize input.

Run `python3 scripts/generate_web_icons.py` with Pillow 13.0.0 to regenerate.
It resizes the mobile launcher with Lanczos to the 64/180-pixel favicon/touch
icons and 192/512-pixel ordinary PWA icons. Maskable sizes resize mobile's
adaptive foreground, preserving its existing two-thirds inset. No additional
padding, recoloring, corner mask or recentering is applied. Native platforms
may apply their own outer icon shape.

Every generated PNG filename contains its SHA256 prefix. The generator updates
all HTML favicon/touch links and manifest icon URLs without changing the app's
identity or start URL. Chrome can skip icon downloads when manifest metadata
is unchanged; replacing bytes at the same URL can leave installed apps stale.
The URL checks in `test/web_manifest_icons.test.js` cover both manifest and HTML
consumers. A large logo change can require Chrome's app-identity confirmation;
verify both an existing installation and a fresh one.

The in-app titlebar displays the text wordmark **Pentacle**; browser tabs, asset
popouts and installed apps use the logo. These surfaces have separate caches.

Rebuilding does not update a running web host's frozen snapshot. After an
authorized host restart, compare the served icon bytes with the built files
and check Chrome's selected favicon. An installed app's icon resources and
visible window/taskbar need a separate check; a correct manifest alone does
not establish that an existing installation refreshed. See
[branding setup](pentacle_setup.md#branding) for the bounded refresh procedure.

The public assistant display fallback is Zephyr. Explicit daemon display names
retain precedence; existing composite addresses, routes and database keys are
protocol identifiers and stay compatible.
