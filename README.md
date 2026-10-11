# FidoraPath Career Workspace

A local-first career portal using the existing FidoraPath backend, SQLite database, login/session handling, scheduled job sync, Freehire international feed, and Jobscanner India feed.

## Start on Windows

1. Install Python 3.11+ from https://www.python.org/downloads/windows/ and enable **Add Python to PATH**.
2. Extract the ZIP and open the `FidoraPath-Portal` folder in VS Code.
3. Copy `.env.example` to `.env`.
4. In `.env`, set `ADMIN_EMAIL`, a unique `ADMIN_PASSWORD` (at least 16 characters), and a random `APP_SECRET` (at least 32 characters). Do not share `.env` or commit it to Git.
5. Open VS Code Terminal in this folder and run `py app.py`.
6. Visit http://127.0.0.1:8765 and sign in with the administrator email/password configured in `.env`.

If `py app.py` reports a missing package, share the error text (never secrets). `requirements.txt` intentionally stays lightweight; PDF extraction uses the backend's built-in fallback if `pypdf` is unavailable.

## Included improvements

- Navy-and-gold FP branding with lightweight CSS transitions, reduced-motion support, animated Career Acceleration hero, service cards, and animated job refresh.
- Career Acceleration service showcase with no prices and a link to https://www.fidorapath.in/.
- Circular animated job match meters and average match summary.
- Resume upload for text-based PDF, DOCX, ODT, RTF, and TXT files (maximum 5 MB), plus paste-in resume text. Scanned PDFs/images may require OCR and may not extract text.
- Country, state/province, city, and broad job-domain filters; available geography options are a curated starting set and can be expanded.
- Date filter for the application tracker.
- Client creation and source-management UI restricted to administrators; client passwords can be at least 4 characters as requested. Use stronger client passwords in real deployments.
- Existing automatic Freehire and Jobscanner India job collection remains in the backend.

## Service content note

The Career Acceleration descriptions reflect FidoraPath services already specified for this project: digital/ATS resumes, LinkedIn optimization, job application assistance and tracking, career guidance/job consultancy, and international job-search support. The website was not accessible to the research tool during this build, so these descriptions have not been verified against the live site's current copy. The service cards link to the official website for confirmation.

## Important deployment notes

- This ZIP contains source code only; it does not contain your `.env`, database, or client records.
- `COOKIE_SECURE=0` is suitable only for local HTTP testing. Use HTTPS and secure cookie settings before public deployment.
- A 4-character password minimum is weak. Prefer longer passwords for client accounts and always use a long, unique administrator password.
- The job match score is a keyword-based estimate, not a hiring prediction. Sponsorship indicators are signals only; confirm eligibility with employers.
- Keep the portal server running while using the local website. Do not expose the development server directly to the public internet.


## Recent portal improvements
- Application overview includes a year-selectable month-by-month application activity chart.
- Application tracker supports a visible from/to date range and clear-filters control.
- Country choices are limited to US, India, United Kingdom, Canada, Germany, and Ireland; state/province and city fields accept typed values as well as suggestions, so unlisted locations can still be searched.
- Job domains cover a broad range of industries and role families.
- Personalized suggestions on Overview are hidden until a resume is available; update a resume to refresh matching.
- Career Acceleration service cards include a WhatsApp contact link to +91 79974 74891. The page includes a sample portfolio link and a sample digital-resume PDF. No service prices are displayed.
- The client-facing interface does not show feed management; backend source endpoints remain admin-only. Existing Freehire and Jobscanner India feeds are preserved.

Sample portfolio: https://sudheerkumarbadatalaportfolio.vercel.app/
