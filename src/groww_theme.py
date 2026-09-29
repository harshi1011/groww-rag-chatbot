"""Groww-inspired page background — CSS only, no dependencies.

Scope: the page canvas behind the app. Nothing here changes a component, a
colour of any text, a border, a button, or a piece of content. The selectors
below are limited to background properties on the outermost containers
(``html``, ``body``, the Streamlit app view), so the foreground UI is left
exactly as Streamlit renders it.

Colours are taken from Groww's own published design system,
``@groww-tech/mint-css`` (the ``MINT`` tokens), so the palette is the brand's
rather than an approximation:

======================  =========  =========================================
Token                   Hex        Role
======================  =========  =========================================
``--green9``            ``#04b488``  brand accent (unused on the canvas)
``--green2``            ``#e9faf3``  ``--background-accent-subtle``
``--green1``            ``#fafefc``  lightest green tint
``--gray50``            ``#f8f8f8``  ``--background-secondary``
======================  =========  =========================================

The canvas is a very soft vertical wash from ``#fafefc`` through ``#f8f8f8`` to
``#e9faf3`` — the Groww neutral/surface range with a faint brand tint at the
foot of the page. It is deliberately near-white: every ratio against the darkest
text in use stays above 13:1, so WCAG AA is met with a wide margin and no text
colour has to change.

Streamlit re-runs a script on every interaction and re-creates the DOM each
time, so this stylesheet is emitted on *every* run. Caching it behind a
session-state flag is wrong: the flag survives the rerun while the markup does
not, which strips the background from the page the moment a user clicks
anything. Emitting unconditionally is idempotent in effect — a duplicate
``<style>`` block applies the same rules — whereas omitting it is not
recoverable.
"""

from __future__ import annotations

import streamlit as st


#: Background-only rules. Read the selectors carefully: each one targets a
#: container, and each declaration is a ``background`` property. There is no
#: ``color``, ``font``, ``border``, ``padding``, or layout declaration in this
#: string by design — the foreground UI must render exactly as before.
_BACKGROUND_CSS = """
<style>
/* --- Groww-inspired page canvas (background only) ------------------------- */
html {
    background-color: #fafefc;
    background-image: linear-gradient(
        180deg,
        #fafefc 0%,    /* green1  - lightest brand tint */
        #f8f8f8 55%,   /* gray50  - Groww surface neutral */
        #e9faf3 100%   /* green2  - accent-subtle */
    );
    background-attachment: fixed;
    background-repeat: no-repeat;
}

body,
.stApp,
[data-testid="stAppViewContainer"] {
    background-color: transparent;
    background-image: none;
}

/* Let the gradient show through the app shell. */
[data-testid="stAppViewContainer"] > .main,
[data-testid="stAppViewContainer"] > section,
[data-testid="stMainBlockContainer"] {
    background-color: transparent;
}

/* Streamlit draws these as opaque white in the default light theme; making
   them transparent is a background change only, so the canvas is continuous
   behind the header and the main column. */
header[data-testid="stHeader"],
#MainMenu,
footer {
    background-color: transparent;
}

/* Preserve the browser default scrollbar styling; nothing to change there. */
</style>
"""


def apply() -> None:
    """Inject the background stylesheet into the page.

    Called on every run, and deliberately not guarded: Streamlit rebuilds the
    page on each rerun, so a stylesheet emitted only once would disappear from
    the DOM after the first interaction.
    """
    st.markdown(_BACKGROUND_CSS, unsafe_allow_html=True)


__all__ = ["apply"]
