# Pentacle branding

The supplied 1024×1024 logo is retained at `renderer/assets/pentacle-logo-source.png`. Its SHA256 is
`6f6533d7585b3532e63ec8226b218e3c47658d212734e3c9ff181f8be7c35117`.
Derive required sizes with Lanczos filtering; preserve the artwork and colors.

Web favicon sizes are 64 and 180 pixels; PWA icons are 192 and 512 pixels.
Maskable variants inset the original square to 80% on its original corner color
so the pentacle fits inside the safe circle. The in-app titlebar displays the
text wordmark **Pentacle**; browser tabs, asset popouts and installed apps use
the logo. These display surfaces have separate browser and installation caches.

Rebuilding does not update a running web host's frozen snapshot. After an
authorized host restart, compare the served icon bytes with the built files
and check Chrome's selected favicon. An installed app's icon resources and
visible window/taskbar need a separate check; a correct manifest alone does
not establish that an existing installation refreshed. See
[branding setup](pentacle_setup.md#branding) for the bounded refresh procedure.

The public assistant display fallback is Zephyr. Explicit daemon display names
retain precedence; existing composite addresses, routes and database keys are
protocol identifiers and stay compatible.
