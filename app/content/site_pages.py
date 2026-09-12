"""
Default title/content for every admin-editable static page, keyed by slug.
This is the source of truth for which page slugs exist at all: a slug
listed here with no SitePage row yet gets lazily created from its default
here (see get_or_create_page in app/routers/pages.py); a SitePage row for a
slug NOT listed here is just orphaned data, never served.

`category` groups pages in the Super Admin "Inhaltsseiten" panel - it has
no effect on the public site.
"""

PLACEHOLDER_NOTICE = (
    '<p class="not-prose bg-amber-50 border border-amber-200 text-amber-800 rounded-lg '
    'p-3 text-sm">Hinweis: Dies ist ein Basis-Entwurf und keine Rechtsberatung. Bitte vor '
    "offizieller Nutzung durch einen Anwalt prüfen und die mit [PLATZHALTER] markierten "
    "Angaben über die Seitenverwaltung im Super-Admin-Bereich ergänzen.</p>"
)

PAGE_DEFINITIONS: dict[str, dict[str, str]] = {
    # --- Rechtliches & Vertrauen ---
    "impressum": {
        "category": "Rechtliches & Vertrauen",
        "title": "Impressum",
        "content": PLACEHOLDER_NOTICE + """
<h1>Impressum</h1>
<h2>Angaben gemäß § 5 TMG</h2>
<p>[PLATZHALTER: Vor- und Nachname bzw. Firmenname]<br>
[PLATZHALTER: Straße und Hausnummer]<br>
[PLATZHALTER: PLZ und Ort]<br>
[PLATZHALTER: Land]</p>
<h2>Kontakt</h2>
<p>E-Mail: <a href="mailto:anasabdulazizde@gmail.com">anasabdulazizde@gmail.com</a><br>
Telefon: [PLATZHALTER: Telefonnummer]</p>
<h2>Umsatzsteuer-ID</h2>
<p>[PLATZHALTER: Umsatzsteuer-Identifikationsnummer gemäß § 27a UStG, falls vorhanden]</p>
<h2>Verantwortlich für den Inhalt nach § 55 Abs. 2 RStV</h2>
<p>[PLATZHALTER: Name und Anschrift]</p>
<h2>Streitschlichtung</h2>
<p>Die Europäische Kommission stellt eine Plattform zur Online-Streitbeilegung (OS) bereit:
<a href="https://ec.europa.eu/consumers/odr/" target="_blank" rel="noopener">https://ec.europa.eu/consumers/odr/</a>.
Wir sind nicht verpflichtet und nicht bereit, an einem Streitbeilegungsverfahren vor einer
Verbraucherschlichtungsstelle teilzunehmen.</p>
""",
    },
    "datenschutz": {
        "category": "Rechtliches & Vertrauen",
        "title": "Datenschutzerklärung",
        "content": PLACEHOLDER_NOTICE + """
<h1>Datenschutzerklärung</h1>
<h2>1. Verantwortlicher</h2>
<p>[PLATZHALTER: Firmenname]<br>
[PLATZHALTER: Anschrift]<br>
E-Mail: [PLATZHALTER: Kontakt-E-Mail]</p>
<h2>2. Welche Daten wir verarbeiten</h2>
<p><strong>Kontodaten:</strong> E-Mail-Adresse, Firmenname, Land, verschlüsseltes Passwort - bei
der Registrierung angelegt.</p>
<p><strong>eBay-Verbindung:</strong> Wenn du dein eBay-Verkäuferkonto verbindest, erhalten wir
über eBays offizielle Schnittstelle (OAuth) Zugriffstoken, die verschlüsselt gespeichert werden,
sowie Bestelldaten (Artikelnummer, Menge, Preis). Wir speichern aktuell keine persönlichen
Käuferdaten (Name, Adresse) aus deinen eBay-Bestellungen.</p>
<p><strong>Produkt- und Geschäftsdaten:</strong> von dir selbst angelegte Produkte,
Lagerbestände, Bestellungen und Ausgaben.</p>
<p><strong>Druck-Agent-Daten:</strong> ein verschlüsselter API-Schlüssel pro registriertem
Print-Agent, zur Authentifizierung des lokalen Druckprogramms.</p>
<h2>3. Zweck der Verarbeitung</h2>
<p>Die Daten werden ausschließlich zur Bereitstellung der Express-Print-Dienste verarbeitet:
Verwaltung von Produkten, Bestellungen, Ausgaben und zum Drucken von Versandetiketten.</p>
<h2>4. Weitergabe an Dritte / Auftragsverarbeiter</h2>
<p>Zum Betrieb dieses Dienstes nutzen wir folgende Dienstleister als Auftragsverarbeiter:</p>
<ul>
<li><strong>Supabase</strong> - Datenbank- und Dateispeicherung</li>
<li><strong>Render</strong> - Hosting der Anwendung</li>
<li><strong>eBay</strong> - zur Synchronisierung deiner Verkäuferbestellungen, sofern du dein
Konto verbindest</li>
</ul>
<p>Eine Weitergabe an sonstige Dritte findet nicht statt.</p>
<h2>5. Speicherdauer</h2>
<p>Daten werden gespeichert, solange dein Konto besteht. Bei Kontolöschung werden deine Daten
auf Anfrage gelöscht, soweit keine gesetzlichen Aufbewahrungspflichten entgegenstehen.</p>
<h2>6. Deine Rechte</h2>
<p>Du hast das Recht auf Auskunft, Berichtigung, Löschung und Einschränkung der Verarbeitung
deiner Daten sowie auf Datenübertragbarkeit. Wende dich dafür an [PLATZHALTER: Kontakt-E-Mail].</p>
<h2>7. Kontakt</h2>
<p>Bei Fragen zum Datenschutz erreichst du uns unter: [PLATZHALTER: Kontakt-E-Mail]</p>
<p class="text-sm text-slate-400">Stand: [PLATZHALTER: Datum]</p>
""",
    },
    "agb": {
        "category": "Rechtliches & Vertrauen",
        "title": "Allgemeine Geschäftsbedingungen (AGB)",
        "content": PLACEHOLDER_NOTICE + """
<h1>Allgemeine Geschäftsbedingungen</h1>
<h2>1. Geltungsbereich</h2>
<p>Diese Allgemeinen Geschäftsbedingungen gelten für alle Verträge zwischen
[PLATZHALTER: Firmenname] ("Anbieter") und Nutzern des Express-Print-Dienstes ("Kunde").</p>
<h2>2. Vertragsgegenstand</h2>
<p>Der Anbieter stellt eine cloudbasierte Software zur Verwaltung von eBay-Bestellungen,
Produkten, Lagerbeständen und zum automatisierten Druck von Versandetiketten bereit.</p>
<h2>3. Vertragsschluss und Laufzeit</h2>
<p>Der Vertrag kommt durch Registrierung eines Kontos und Auswahl eines Preisplans zustande.
Abonnements verlängern sich automatisch monatlich, sofern nicht fristgerecht gekündigt wird.</p>
<h2>4. Preise und Zahlungsbedingungen</h2>
<p>Es gelten die zum Zeitpunkt des Vertragsschlusses auf der <a href="pricing.html">Preisseite</a>
ausgewiesenen Preise zzgl. gesetzlicher Umsatzsteuer.</p>
<h2>5. Kündigung</h2>
<p>Der Kunde kann sein Abonnement jederzeit zum Ende der laufenden Abrechnungsperiode kündigen.
Für Verbraucher gilt zusätzlich das <a href="widerruf.html">Widerrufsrecht</a>.</p>
<h2>6. Haftung</h2>
<p>Der Anbieter haftet unbeschränkt für Vorsatz und grobe Fahrlässigkeit sowie nach Maßgabe des
Produkthaftungsgesetzes. Im Übrigen ist die Haftung auf vorhersehbare, vertragstypische Schäden
begrenzt.</p>
<h2>7. Datenschutz</h2>
<p>Es gilt unsere <a href="privacy.html">Datenschutzerklärung</a>.</p>
<h2>8. Schlussbestimmungen</h2>
<p>Es gilt das Recht der Bundesrepublik Deutschland. Gerichtsstand ist, soweit gesetzlich
zulässig, [PLATZHALTER: Sitz des Anbieters].</p>
<p class="text-sm text-slate-400">Stand: [PLATZHALTER: Datum]</p>
""",
    },
    "widerruf": {
        "category": "Rechtliches & Vertrauen",
        "title": "Widerrufsrecht",
        "content": PLACEHOLDER_NOTICE + """
<h1>Widerrufsrecht</h1>
<h2>Widerrufsrecht für Verbraucher</h2>
<p>Verbrauchern steht ein gesetzliches Widerrufsrecht zu. Sie haben das Recht, binnen
vierzehn Tagen ohne Angabe von Gründen diesen Vertrag zu widerrufen.</p>
<p>Die Widerrufsfrist beträgt vierzehn Tage ab Vertragsschluss (Abschluss des Abonnements).</p>
<p>Um Ihr Widerrufsrecht auszuüben, müssen Sie uns ([PLATZHALTER: Firmenname, Anschrift],
E-Mail: <a href="mailto:anasabdulazizde@gmail.com">anasabdulazizde@gmail.com</a>) mittels
einer eindeutigen Erklärung (z. B. per Post versandter Brief oder E-Mail) über Ihren
Entschluss, diesen Vertrag zu widerrufen, informieren.</p>
<h2>Folgen des Widerrufs</h2>
<p>Wenn Sie diesen Vertrag widerrufen, erstatten wir Ihnen alle Zahlungen, die wir von Ihnen
erhalten haben, unverzüglich und spätestens binnen vierzehn Tagen ab dem Tag, an dem die
Mitteilung über Ihren Widerruf bei uns eingegangen ist.</p>
<p>Haben Sie verlangt, dass die Dienstleistung während der Widerrufsfrist beginnen soll, so
haben Sie uns einen angemessenen Betrag zu zahlen, der dem Anteil der bis zum Zeitpunkt der
Widerrufserklärung bereits erbrachten Leistungen entspricht.</p>
<h2>Muster-Widerrufsformular</h2>
<p>(Wenn Sie den Vertrag widerrufen wollen, füllen Sie bitte dieses Formular aus und senden
Sie es zurück.)</p>
<p>An [PLATZHALTER: Firmenname, Anschrift, E-Mail]:<br>
Hiermit widerrufe(n) ich/wir den von mir/uns abgeschlossenen Vertrag über die Nutzung des
Express-Print-Abonnements.<br>
Bestellt am: __________<br>
Name des/der Verbraucher(s): __________<br>
Anschrift des/der Verbraucher(s): __________<br>
Datum: __________</p>
""",
    },
    "cookies": {
        "category": "Rechtliches & Vertrauen",
        "title": "Cookie-Hinweis",
        "content": """
<h1>Cookie-Hinweis</h1>
<p>Diese Website verwendet ausschließlich technisch notwendige Cookies bzw. Speicherung, die
für den Betrieb und die Sicherheit des Dienstes erforderlich sind - insbesondere zur
Aufrechterhaltung Ihrer Anmeldesitzung.</p>
<h2>Was wir speichern</h2>
<ul>
<li><strong>Sitzungs-Token (localStorage):</strong> speichert Ihren Anmeldestatus, damit Sie
nicht bei jedem Seitenaufruf erneut ein Passwort eingeben müssen.</li>
</ul>
<h2>Keine Tracking- oder Marketing-Cookies</h2>
<p>Wir setzen aktuell keine Analyse-, Tracking- oder Werbe-Cookies Dritter ein. Sollte sich
dies ändern, informieren wir an dieser Stelle und holen, soweit gesetzlich erforderlich,
Ihre Einwilligung ein.</p>
<p>Weitere Informationen zur Datenverarbeitung finden Sie in unserer
<a href="privacy.html">Datenschutzerklärung</a>.</p>
""",
    },
    # --- Unternehmen & Vertrauen ---
    "about": {
        "category": "Unternehmen & Vertrauen",
        "title": "Über uns",
        "content": """
<h1>Über uns</h1>
<p>Express Print wurde aus einem ganz konkreten Problem heraus gebaut: Als eBay-Verkäufer
verbringt man zu viel Zeit mit manuellem Etikettendruck, Bestell-Copy-Paste und
Excel-Tabellen für Ausgaben und Lagerbestand - Zeit, die im Kerngeschäft fehlt.</p>
<p>Wir bauen eine schlanke, fokussierte Software, die genau das automatisiert:
eBay-Bestellungen synchronisieren, Versandetiketten ohne manuelles Zutun drucken, und
Lagerbestand sowie Ausgaben an einem Ort im Blick behalten.</p>
<h2>Unsere Herangehensweise</h2>
<ul>
<li>Fokus auf die Kernprobleme von eBay-Verkäufern statt auf möglichst viele Funktionen.</li>
<li>Direkter Kontakt zu unseren Nutzern - Feedback fließt schnell in neue Funktionen ein.</li>
<li>Faire, transparente Preise ohne versteckte Kosten.</li>
</ul>
<p>Fragen oder Feedback? <a href="contact.html">Kontaktiere uns</a> - wir freuen uns, von dir
zu hören.</p>
""",
    },
    "contact": {
        "category": "Unternehmen & Vertrauen",
        "title": "Kontakt",
        "content": """
<h1>Kontakt</h1>
<p>Wir sind für dich da - schreib uns einfach, wir antworten in der Regel innerhalb eines
Werktags.</p>
<h2>E-Mail</h2>
<p><a href="mailto:anasabdulazizde@gmail.com">anasabdulazizde@gmail.com</a></p>
<h2>Postanschrift</h2>
<p>[PLATZHALTER: Firmenname]<br>
[PLATZHALTER: Straße und Hausnummer]<br>
[PLATZHALTER: PLZ und Ort]</p>
<p>Für rechnungs- oder vertragsbezogene Anfragen findest du weitere Angaben im
<a href="impressum.html">Impressum</a>.</p>
""",
    },
    "careers": {
        "category": "Unternehmen & Vertrauen",
        "title": "Karriere",
        "content": """
<h1>Karriere</h1>
<p>Express Print wächst - aktuell haben wir keine offenen Stellen ausgeschrieben, freuen uns
aber jederzeit über Initiativbewerbungen.</p>
<h2>Warum bei uns arbeiten?</h2>
<ul>
<li>Direkter Einfluss auf ein Produkt, das echten eBay-Verkäufern täglich Zeit spart.</li>
<li>Kurze Entscheidungswege, keine Bürokratie.</li>
<li>Remote-freundlich.</li>
</ul>
<p>Interesse? Schick uns eine kurze Nachricht an
<a href="mailto:anasabdulazizde@gmail.com">anasabdulazizde@gmail.com</a> - gerne mit ein
paar Zeilen zu dir und was dich reizt.</p>
""",
    },
    # --- Support & Produktinfo ---
    "faq": {
        "category": "Support & Produktinfo",
        "title": "Häufige Fragen",
        "content": """
<h1>Häufige Fragen</h1>
<h2>Was ist Express Print?</h2>
<p>Eine Software für eBay-Verkäufer, die Bestellungen synchronisiert, Versandetiketten
automatisch druckt und Lagerbestand sowie Ausgaben verwaltet.</p>
<h2>Brauche ich einen eigenen Drucker?</h2>
<p>Ja - Express Print steuert deinen bestehenden Etikettendrucker über den lokal
installierten Print-Agent an.</p>
<h2>Kann ich jederzeit kündigen?</h2>
<p>Ja, dein Abonnement ist monatlich kündbar. Details findest du in den
<a href="agb.html">AGB</a> und im <a href="widerruf.html">Widerrufsrecht</a>.</p>
<h2>Unterstützt ihr andere Marktplätze außer eBay?</h2>
<p>Aktuell liegt unser Fokus auf eBay. Weitere Integrationen sind auf unserer
<a href="integrations.html">Integrationen</a>-Seite und im
<a href="changelog.html">Änderungsprotokoll</a> zu sehen, sobald sie verfügbar sind.</p>
<h2>Wo bekomme ich weitere Hilfe?</h2>
<p>Im <a href="help.html">Hilfe-Center</a> oder direkt per <a href="contact.html">Kontakt</a>.</p>
""",
    },
    "help": {
        "category": "Support & Produktinfo",
        "title": "Hilfe-Center",
        "content": """
<h1>Hilfe-Center</h1>
<p>Hier findest du die schnellsten Wege zu Antworten:</p>
<ul>
<li><a href="faq.html">Häufige Fragen (FAQ)</a> - Antworten auf die häufigsten Fragen.</li>
<li><a href="status.html">Systemstatus</a> - aktueller Betriebsstatus von Express Print.</li>
<li><a href="changelog.html">Änderungsprotokoll</a> - was sich zuletzt geändert hat.</li>
<li><a href="mailto:anasabdulazizde@gmail.com">Direkter E-Mail-Support</a> - für alles, was
hier nicht beantwortet wird.</li>
</ul>
<h2>Print-Agent-Probleme</h2>
<p>Prüfe zuerst, ob dein Print-Agent im Dashboard unter "Print-Agents" als
<strong>online</strong> angezeigt wird. Ist er offline, starte das lokale Programm auf dem
Rechner mit dem angeschlossenen Drucker neu.</p>
""",
    },
    "status": {
        "category": "Support & Produktinfo",
        "title": "Systemstatus",
        "content": """
<h1>Systemstatus</h1>
<div class="not-prose flex items-center gap-3 bg-emerald-50 border border-emerald-200
text-emerald-700 rounded-lg p-4 mb-6">
<span class="w-3 h-3 rounded-full bg-emerald-500"></span>
<span class="font-semibold">Alle Systeme funktionieren normal</span>
</div>
<p>Diese Seite wird manuell durch das Express-Print-Team aktualisiert. Bei größeren
Störungen informieren wir hier sowie per E-Mail an betroffene Nutzer.</p>
<h2>Komponenten</h2>
<ul>
<li>API &amp; Dashboard - Betriebsbereit</li>
<li>eBay-Synchronisierung - Betriebsbereit</li>
<li>Print-Agent-Verbindungen - Betriebsbereit</li>
</ul>
<p class="text-sm text-slate-400">Hinweis: Der API-Server läuft im Free Tier von Render und
kann nach Inaktivität einige Sekunden zum Aufwachen benötigen - das ist kein Ausfall.</p>
""",
    },
    "changelog": {
        "category": "Support & Produktinfo",
        "title": "Änderungsprotokoll",
        "content": """
<h1>Änderungsprotokoll</h1>
<h2>2026-09</h2>
<ul>
<li>Neue Preisseite mit live geladenen Plänen.</li>
<li>Super-Admin-Bereich zur Verwaltung von Plänen, Logo und Inhaltsseiten.</li>
</ul>
<h2>Bald verfügbar</h2>
<ul>
<li>Weitere Marktplatz-Integrationen.</li>
<li>Erweiterte Marktrecherche-Funktionen.</li>
</ul>
<p class="text-sm text-slate-400">Diese Seite wird laufend über die Seitenverwaltung im
Super-Admin-Bereich aktualisiert.</p>
""",
    },
    # --- Wachstum & Conversion ---
    "integrations": {
        "category": "Wachstum & Conversion",
        "title": "Integrationen",
        "content": """
<h1>Integrationen</h1>
<p>Express Print verbindet sich direkt mit den Werkzeugen, die du als eBay-Verkäufer
bereits nutzt.</p>
<h2>Verfügbar</h2>
<ul>
<li><strong>eBay</strong> - automatische Synchronisierung deiner Bestellungen über die
offizielle eBay-API.</li>
<li><strong>Lokale Etikettendrucker</strong> - über den Express-Print-Agent, kompatibel mit
den meisten Windows-Etikettendruckern.</li>
</ul>
<h2>Geplant</h2>
<p>Weitere Marktplatz- und Buchhaltungs-Integrationen sind in Planung. Wünsche?
<a href="contact.html">Schreib uns</a>.</p>
""",
    },
    "customers": {
        "category": "Wachstum & Conversion",
        "title": "Erfolgsgeschichten",
        "content": """
<h1>Erfolgsgeschichten</h1>
<p class="not-prose bg-amber-50 border border-amber-200 text-amber-800 rounded-lg p-3
text-sm">Platzhalter-Inhalt - hier können echte Kundenstimmen ergänzt werden, sobald
verfügbar.</p>
<blockquote>"Express Print hat uns pro Tag mehrere Stunden manuellen Etikettendruck
erspart." - [PLATZHALTER: Kundenname, Unternehmen]</blockquote>
<p>Bist du Kunde und möchtest deine Geschichte teilen?
<a href="contact.html">Wir freuen uns über deine Nachricht</a>.</p>
""",
    },
    "partners": {
        "category": "Wachstum & Conversion",
        "title": "Partnerprogramm",
        "content": """
<h1>Partnerprogramm</h1>
<p>Du berätst eBay-Verkäufer oder betreibst eine Agentur im E-Commerce-Umfeld? Als
Express-Print-Partner empfiehlst du unseren Dienst weiter und profitierst davon.</p>
<h2>Wie es funktioniert</h2>
<ul>
<li>Du empfiehlst Express Print an deine Kunden oder dein Netzwerk.</li>
<li>Wir stimmen die Konditionen individuell mit dir ab.</li>
</ul>
<p>Interesse? Melde dich unter
<a href="mailto:anasabdulazizde@gmail.com">anasabdulazizde@gmail.com</a> - wir melden uns
zeitnah zurück.</p>
""",
    },
    "blog": {
        "category": "Wachstum & Conversion",
        "title": "Blog",
        "content": """
<h1>Blog</h1>
<p class="not-prose bg-amber-50 border border-amber-200 text-amber-800 rounded-lg p-3
text-sm">Der Blog startet in Kürze.</p>
<p>Hier werden künftig Tipps rund um eBay-Verkauf, Versand und Lagerverwaltung sowie
Produkt-Neuigkeiten geteilt.</p>
""",
    },
}
