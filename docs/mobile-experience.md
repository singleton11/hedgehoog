# Mobile experience: first pass

At widths up to 600px, workspace navigation collapses into a sticky Menu control
and the acquired ETF inventory becomes labeled cards. No inventory fields are
removed. Historical and statistical annual return/volatility remain explicitly
distinguished, and existing update/removal consent flows are unchanged.

The cards reuse the desktop table's data and actions rather than issuing extra
requests or maintaining a separate mobile list. Explicit table roles and the
visually hidden column headers preserve table semantics; visible card labels
are hidden from assistive technology to avoid duplicate announcements.

Update all lives beside Fund overview, outside the table, so it stays reachable
on phones and does not require scrolling to the far-right desktop column.
Tablet/desktop retain the full table and navigation links.

Phone controls have a minimum 44px height, text inputs/selects use 16px type to
avoid iOS focus zoom, and customer/portfolio tabs scroll within their own row.
Other dense tables and charts still use their existing horizontal scrolling;
this is an incremental improvement, not a complete mobile redesign.

## Regression checks

Run `npm test -- --run src/App.test.jsx src/Instruments.test.jsx` and
`npm run build` from `frontend/`.

For browser checks, start the app with `make run` and use responsive mode at
320, 375, 390, 600, 768 and 1280 CSS pixels:

- Sign in: inputs fit the screen, use 16px text on phones, and remain zoomable.
- On phones, navigation starts collapsed and shows the current workspace section.
  Open Menu and navigate to every section. The selected link closes the menu,
  including when selecting the current page. Browser back/forward closes it too.
- With the menu open, press Escape: it closes and restores focus to Menu.
  Collapsed links must not receive keyboard focus. Skip to content still works.
- In the acquired ETF list, use a long fund/index name and a fund without quotes.
  Cards should fit without page-level horizontal scrolling. Check all ten field
  labels, signed returns, unavailable values, listing, dates and coverage.
- Check Update all, per-fund update/removal confirmation and cancellation, history
  filtering, and Previous/Next. Touch buttons should be at least 44px tall.
- On tablet/desktop, navigation links remain visible without Menu and the inventory
  remains a horizontally scrollable table without mobile labels.
- Open customer and portfolio pages: long tab rows should scroll locally, while
  forms remain single-column on phones. Check portrait/landscape rotation.

Implementation validation used headless Chromium with mocked API responses at
all six widths for inventory overflow, menu visibility/expansion/Escape, focus,
card/table switching, action sizes, update consent and phone login input sizing.
Real-device Safari/VoiceOver and TalkBack still need manual verification.
