"""
The legal pages as published text. NettoDesk is offered to businesses only
(§ 14 BGB); sign-up asks to confirm that. {{operator_*}} is filled in when a
page is shown (see operator_service).

These texts describe what the software does. They are not legal advice and have
not been reviewed by a lawyer.
"""

from app.config import settings

TERMS_VERSION = "2026-11-25"

IMPRESSUM = """<h1>Impressum</h1>
<h2>Angaben gemäß § 5 DDG</h2>
<p>{{operator_name}}<br>
{{operator_street}}<br>
{{operator_city}}<br>
{{operator_country}}</p>
<h2>Kontakt</h2>
<p>E-Mail: {{operator_email}}{{#phone}}<br>
Telefon: {{operator_phone}}{{/phone}}</p>
{{#vat_id}}<h2>Umsatzsteuer-Identifikationsnummer</h2>
<p>{{operator_vat_id}}</p>
{{/vat_id}}<h2>Verbraucherstreitbeilegung</h2>
<p>Wir sind nicht verpflichtet und nicht bereit, an einem Streitbeilegungsverfahren vor einer
Verbraucherschlichtungsstelle teilzunehmen.</p>
"""

CONTACT = """<h1>Kontakt</h1>
<p>Wir helfen dir gerne weiter.</p>
<p>E-Mail: {{operator_email}}{{#phone}}<br>
Telefon: {{operator_phone}}{{/phone}}</p>
<p>Alle Anbieterangaben findest du im <a href="impressum.html">Impressum</a>.</p>
"""

WITHDRAWAL = """<h1>Widerrufsrecht</h1>
<p>NettoDesk richtet sich ausschließlich an <strong>Unternehmer</strong> im Sinne des § 14 BGB, also an
Personen und Gesellschaften, die beim Vertragsschluss in Ausübung ihrer gewerblichen oder
selbständigen beruflichen Tätigkeit handeln. Verbraucher können kein Konto anlegen und keinen
Vertrag über NettoDesk schließen. Das bei der Registrierung bestätigt jeder Kunde.</p>
<p>Ein gesetzliches Widerrufsrecht für Verbraucher besteht für diese Verträge daher nicht.</p>
<p>Wie du kündigen kannst, steht in § 5 der <a href="policies.html">AGB</a>.</p>
<p>Fragen: {{operator_email}}</p>
"""


def _price_sentence() -> str:
    if settings.operator_small_business:
        return "Auf die Preise wird gemäß § 19 UStG (Kleinunternehmer) keine Umsatzsteuer berechnet."
    return "Die Preise verstehen sich zuzüglich der gesetzlichen Umsatzsteuer."


def policies() -> str:
    return """<h1>Allgemeine Geschäftsbedingungen (AGB)</h1>
<h2>§ 1 Geltungsbereich und Vertragspartner</h2>
<p>Diese Bedingungen gelten für alle Verträge über die Nutzung der Software NettoDesk zwischen
{{operator_name}}, {{operator_street}}, {{operator_city}} („Anbieter“) und dem Kunden. NettoDesk wird
ausschließlich Unternehmern im Sinne des § 14 BGB angeboten. Verträge mit Verbrauchern werden nicht
geschlossen.</p>
<h2>§ 2 Leistung</h2>
<p>NettoDesk ist eine über das Internet genutzte Software zur Verwaltung von Bestellungen, Produkten,
Lagerbestand, Versandetiketten, Rechnungen und Buchhaltungsunterlagen von Online-Händlern. Der Umfang
richtet sich nach dem gebuchten Plan. Auswertungen zu Steuern und für Behörden (z. B. EÜR, Anlage EKS)
sind Rechenhilfen aus den erfassten Daten. Sie sind keine Steuer- oder Rechtsberatung; der Kunde prüft
sie vor der Verwendung. Verbindungen zu Diensten Dritter (z. B. eBay, DHL) hängen von deren
Verfügbarkeit und Bedingungen ab.</p>
<h2>§ 3 Konto</h2>
<p>Für die Nutzung ist eine Registrierung mit zutreffenden Angaben erforderlich. Der Kunde hält seine
Zugangsdaten geheim und informiert den Anbieter, wenn er einen Missbrauch vermutet.</p>
<h2>§ 4 Preise, Testphase und Zahlung</h2>
<p>Es gelten die beim Vertragsschluss auf der <a href="pricing.html">Preisseite</a> angegebenen
monatlichen Preise. """ + _price_sentence() + """ Die Abrechnung erfolgt monatlich im Voraus. Eine
kostenlose Testphase, sofern angeboten, endet ohne Kündigung und ohne Zahlungspflicht; danach ist die
Nutzung nur mit einem gebuchten Plan möglich.</p>
<h2>§ 5 Laufzeit und Kündigung</h2>
<p>Der Vertrag läuft auf unbestimmte Zeit. Beide Seiten können ihn jederzeit zum Ende der laufenden
monatlichen Abrechnungsperiode in Textform (z. B. per E-Mail) kündigen. Das Recht zur Kündigung aus
wichtigem Grund bleibt unberührt. Der Kunde sichert vor dem Vertragsende die Unterlagen, die er
aufbewahren muss; NettoDesk stellt dafür Exportfunktionen bereit.</p>
<h2>§ 6 Verfügbarkeit und Änderungen des Dienstes</h2>
<p>Der Anbieter bemüht sich um eine hohe Verfügbarkeit, sichert aber keine ununterbrochene
Erreichbarkeit zu. Er darf den Funktionsumfang mit angemessener Ankündigung anpassen, soweit der
vertragliche Zweck erhalten bleibt. NettoDesk ist kein revisionssicheres Archiv; eigene
Aufbewahrungspflichten des Kunden bleiben bestehen.</p>
<h2>§ 7 Pflichten des Kunden</h2>
<p>Der Kunde nutzt NettoDesk nur rechtmäßig, hält die Bedingungen der verbundenen Dienste ein und ist
für die Rechtmäßigkeit der Daten verantwortlich, die er verarbeiten lässt, einschließlich der Daten
seiner Käufer.</p>
<h2>§ 8 Daten und Datenschutz</h2>
<p>Die Daten des Kunden bleiben seine Daten. Für personenbezogene Daten, die NettoDesk im Auftrag des
Kunden verarbeitet, gilt der <a href="avv.html">Vertrag zur Auftragsverarbeitung</a>; er ist Bestandteil
dieses Vertrags. Im Übrigen gilt die <a href="privacy.html">Datenschutzerklärung</a>.</p>
<h2>§ 9 Haftung</h2>
<p>Der Anbieter haftet unbeschränkt bei Vorsatz und grober Fahrlässigkeit, bei Verletzung des Lebens,
des Körpers oder der Gesundheit sowie nach dem Produkthaftungsgesetz. Bei leicht fahrlässiger
Verletzung einer wesentlichen Vertragspflicht ist die Haftung auf den vertragstypischen,
vorhersehbaren Schaden begrenzt. Im Übrigen ist die Haftung bei leichter Fahrlässigkeit ausgeschlossen.</p>
<h2>§ 10 Änderungen dieser Bedingungen</h2>
<p>Änderungen werden dem Kunden in Textform mitgeteilt und gelten für bestehende Verträge nur, wenn der
Kunde zustimmt. Ohne Zustimmung gilt die bisherige Fassung weiter; das Kündigungsrecht nach § 5 bleibt
für beide Seiten bestehen.</p>
<h2>§ 11 Schlussbestimmungen</h2>
<p>Es gilt deutsches Recht unter Ausschluss des UN-Kaufrechts. Ist der Kunde Kaufmann, ist
Gerichtsstand der Sitz des Anbieters. Sollten einzelne Bestimmungen unwirksam sein, bleibt der Vertrag
im Übrigen wirksam.</p>
<p>Stand: """ + "25.11.2026" + """</p>
"""


COOKIES = """<h1>Cookie-Hinweis</h1>
<p>NettoDesk setzt keine Analyse-, Tracking- oder Werbe-Cookies ein. Im lokalen Speicher deines Browsers
liegen nur Angaben, die für die von dir gewünschten Funktionen nötig sind (§ 25 Abs. 2 TDDDG):</p>
<ul>
<li>dein Anmeldestatus (auch für das Partnerportal),</li>
<li>die gewählte Sprache,</li>
<li>deine Einstellungen für den Etikettendruck,</li>
<li>dass du diesen Hinweis geschlossen hast.</li>
</ul>
<p>Mehr dazu in der <a href="privacy.html">Datenschutzerklärung</a>.</p>
"""

STATUS = """<h1>Systemstatus</h1>
<p>Diese Seite wird nicht automatisch aktualisiert und zeigt keinen Live-Status. Wenn etwas nicht
funktioniert, schreib uns über die <a href="contact.html">Kontaktseite</a>.</p>
<p>Der Server kann nach längerer Inaktivität einige Sekunden zum Aufwachen brauchen.</p>
"""
