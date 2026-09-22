# Interface fonts

Self-hosted Latin variable subsets, downloaded from Google Fonts:

- **DM Sans**, weights 400–700: https://fonts.google.com/specimen/DM+Sans
- **Manrope**, weights 400–800: https://fonts.google.com/specimen/Manrope

Both use the SIL Open Font License. License files live in `frontend/public/font-licenses/` so Vite includes them in the production build. Characters outside these subsets use the system font fallback. The interface makes no runtime Google Fonts requests.
