CAREFIND AI — AI Based Nearby Hospital and Doctor Recommendation System
=====================================================================

Academic mini-project built with Flask, SQLite, one HTML/CSS/JavaScript
frontend, OpenStreetMap, Leaflet and the public OSRM driving router.

The login and dashboard share a cool-toned medical photo background with a
readability overlay. The image is served from Unsplash and therefore needs an
internet connection. To use a supplied local image instead, replace the
images.unsplash.com URL in the body:before background rules in index.html with
the image URL or data URI you choose; the photo background stays behind all
three role-based dashboards.

RUN IT IN VS CODE (Windows PowerShell)
-------------------------------------
1. Install Python 3.10 or later and Visual Studio Code.
2. Open this project folder in VS Code.
3. Open Terminal > New Terminal. Confirm the terminal is PowerShell and
   its current directory is this folder (the folder containing app.py).
4. Create and activate a virtual environment:

       py -m venv .venv
       .\.venv\Scripts\Activate.ps1

   If PowerShell blocks activation, use this terminal-only alternative:

       Set-ExecutionPolicy -Scope Process -ExecutionPolicy Bypass
       .\.venv\Scripts\Activate.ps1

5. Install the one Python dependency:

       python -m pip install -r requirements.txt

6. (Optional) Set a persistent secret for local development:

       $env:SECRET_KEY = "replace-with-a-long-random-secret"

   Use a secure, unique secret and HTTPS in any deployment. For HTTPS,
   set $env:COOKIE_SECURE = "1". Never expose the development server to the
   public internet or commit production secrets.

7. Start the server:

       python app.py

8. Open http://127.0.0.1:5000 in a browser. Use Ctrl+C in the terminal to
   stop the server. On first launch, SQLite tables and clearly marked sample
   accounts are created automatically in healthcare.db.

DEMO SIGN-INS
-------------
Patient:  patient@example.com  / Patient123!
Hospital: hospital@example.com / Hospital123!
Admin:    admin@example.com    / Admin123!

Choose the matching account role on the sign-in page. Hospital staff use the
separate Hospital staff sign-in link. New patient accounts require a phone
number; the server validates the selected role and stores the number in SQLite.

The demo hospital and doctor are fictional academic records, explicitly
marked SAMPLE ONLY in the interface. The demo doctor's initial status is
Unavailable and is labeled sample data, not live availability. Hospital staff
can change the demo status to test the patient update and booking workflow.
Real facilities must register and be verified by an administrator before
their profile can be represented as verified.
Doctor photographs can be uploaded by hospital staff as PNG/JPEG files
(maximum 5 MB); patient prescription files are separate and private.

WORKFLOW TO TRY
---------------
1. Sign in as patient. Allow GPS in the browser, or search for a city/area.
   The site can also be used with GPS denied.
2. Select symptoms, duration and severity; request a specialist suggestion
   and nearby facility listings.
3. Inspect the explainable score, sample-data labels, provider information
   and map. Select a hospital and request directions.
4. Sign in separately as hospital staff. Add a doctor or change the demo
   doctor's status to Available. Patient status changes are polled about
   every 15 seconds while the Find Care page is open.
5. Return to the patient account, request an available doctor's appointment,
   and review its pending status. Hospital staff can accept, reject or
   reschedule the request. A doctor/date/time slot cannot have two active
   pending or accepted bookings.
6. Sign in as admin to review/verify facilities, manage accounts and resolve
   incorrect-information reports. Try patient medical notes, allergy fields,
   private prescription uploads, favourites and feedback.

Accepted appointments receive a one-time reminder notification when the
patient next opens the dashboard or appointment list within 24 hours of the
appointment. This local prototype does not run a background notification
service while the application is closed.

LOCATION, MAPS AND EXTERNAL SERVICES
------------------------------------
No API key is required. Location permission is optional. When GPS is allowed,
the browser's coordinates are sent to OpenStreetMap Nominatim for reverse
geocoding, and the server queries OpenStreetMap Overpass for nearby
hospital/clinic listings; Nominatim search is a no-key fallback when Overpass
is unavailable or returns no nearby facilities. Manual place search also uses
Nominatim. These public,
community-maintained services can be incomplete, rate-limited, or offline;
errors are shown and saved platform records remain available. Coordinates
are used for discovery and are not saved to the patient's profile.

Leaflet displays OpenStreetMap tiles. The public OSRM endpoint provides the
actual returned driving route, distance and estimated time. This public OSRM
instance supports driving only; no walking or cycling route is fabricated.
Google Maps can be opened as an external navigation fallback, including for
other travel modes. Straight-line proximity is clearly labeled and is not
presented as road distance or travel time.

OpenStreetMap names, addresses, contact data and tags are community data,
not official verification. Ratings are not invented. This project does not
currently display aggregate public ratings; patient-submitted feedback is
shown as visit-verified only when linked to an accepted appointment. External
OSM listings do not include live provider availability. Doctor availability
is shown only for platform directory entries and is explicitly staff-reported.

PRIVACY, SECURITY AND LIMITATIONS
---------------------------------
This academic prototype is not a medical device, diagnostic tool,
appointment provider, emergency service, or substitute for professional
medical advice. Specialist suggestions use transparent, deterministic
symptom rules; they are not a trained or clinically validated AI model. The
emergency symptom warning is precautionary and not a complete triage system.
For emergencies, contact local emergency services immediately.

Passwords use Werkzeug's password hashing. API writes require a session CSRF
token; role checks protect staff/admin operations. Sessions use HTTP-only,
SameSite cookies. Prescription files are stored outside the static web root
and served only to the patient who uploaded them (or an admin). Uploads are
limited to PDF/JPEG/PNG and 5 MB. SQLite and uploads are local files; protect
the machine and back them up securely. Do not enter real patient information
in the demo. This project has not undergone clinical, privacy, security,
accessibility, or regulatory certification. Use HTTPS and a strong persistent
SECRET_KEY for any deployment; the built-in server is for local development.

The application requests no third-party API key. Public mapping services
have their own terms, privacy policies, attribution rules, and fair-use
limits. Do not run high-volume queries against public endpoints.

The English/Kannada switch translates the main navigation, key headings,
account controls and the symptom list. Some detailed forms and information
text remain in English.

PROJECT FILES
-------------
index.html       Complete responsive frontend, styles and client application.
app.py           Flask API, authentication, SQLite schema, seed data and logic.
requirements.txt Python dependency list.
README.txt       Setup, test workflow, safety and service disclosures.
healthcare.db    Automatically created on first run; do not commit real data.
private_uploads  Created on first run for private patient prescription files.
doctor_photos    Created on first run for hospital-managed doctor photographs.

For a fresh local academic database, stop the server and remove only the
project's healthcare.db file after making any backups you need. Restarting
the application recreates the schema and demo accounts. Private uploads are
not deleted when the database is reset; inspect and remove only files you
intend to discard.
