from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from app.core.deps import get_db
from app.models.payment_method import PaymentMethod
from app.models.plan import Plan, SiteSettings
from app.models.site_page import SitePage
from app.schemas.admin import PaymentMethodOut, PlanOut, SiteSettingsOut
from app.schemas.site_page import SitePageOut

router = APIRouter(tags=["public"])

# The fixed, known set of admin-editable content pages (see app/models/site_page.py's
# docstring for why this isn't an open-ended CMS). Placeholder copy, meant to
# be rewritten from the Super Admin panel before real customers see it.
DEFAULT_SITE_PAGES = {
    "about": {
        "title": "Über uns",
        "content": (
            "ShipSync hilft eBay-Verkäufern, Bestellungen, Produkte und den Versand an einem Ort zu verwalten.\n\n"
            "[PLATZHALTER: Erzähle hier, wer ihr seid und warum ihr ShipSync gebaut habt.]"
        ),
    },
    "policies": {
        "title": "Allgemeine Geschäftsbedingungen (AGB)",
        "content": (
            "Hinweis: Dies ist ein Basis-Entwurf und keine Rechtsberatung. Bitte vor "
            "offizieller Nutzung durch einen Anwalt prüfen und alle [PLATZHALTER] ergänzen.\n\n"
            "§ 1 Geltungsbereich und Vertragspartner\n"
            "Diese Allgemeinen Geschäftsbedingungen gelten für alle Verträge zwischen "
            "[PLATZHALTER: Firmenname], [PLATZHALTER: Anschrift] (\"wir\", \"ShipSync\") und "
            "Unternehmern im Sinne des § 14 BGB (\"Kunde\") über die Nutzung der Software ShipSync.\n\n"
            "§ 2 Vertragsgegenstand und Leistungsbeschreibung\n"
            "ShipSync ist eine cloud-basierte Software (SaaS) zur Verwaltung von Bestellungen, "
            "Produkten, Lagerbestand und Versandetiketten für eBay-Verkäufer, einschließlich der "
            "Anbindung an eBay und optional an Versanddienstleister wie DHL. Der genaue "
            "Funktionsumfang richtet sich nach dem gebuchten Plan (siehe /pricing.html).\n\n"
            "§ 3 Registrierung und Nutzerkonto\n"
            "Für die Nutzung ist eine Registrierung mit zutreffenden Angaben erforderlich. Der "
            "Kunde ist für die Geheimhaltung seiner Zugangsdaten und für alle Aktivitäten unter "
            "seinem Konto verantwortlich.\n\n"
            "§ 4 Preise und Zahlungsbedingungen\n"
            "Es gelten die zum Zeitpunkt des Vertragsschlusses auf /pricing.html angegebenen "
            "Preise, jeweils zzgl. gesetzlicher Umsatzsteuer. Die Abrechnung erfolgt "
            "[PLATZHALTER: monatlich/jährlich im Voraus]. [PLATZHALTER: Angaben zu kostenlosen "
            "Testphasen, falls angeboten.]\n\n"
            "§ 5 Laufzeit und Kündigung\n"
            "Der Vertrag läuft auf unbestimmte Zeit und kann von beiden Seiten mit einer Frist "
            "von [PLATZHALTER: z. B. einem Monat] zum Ende der jeweiligen Abrechnungsperiode "
            "gekündigt werden. Das Recht zur außerordentlichen Kündigung aus wichtigem Grund "
            "bleibt unberührt. Verbrauchern und Unternehmern, die einen Vertrag über eine "
            "Dauerleistung außerhalb von Geschäftsräumen oder im Fernabsatz schließen, steht "
            "zusätzlich das gesetzliche Widerrufsrecht zu (siehe /withdrawal.html).\n\n"
            "§ 6 Verfügbarkeit und Änderungen des Dienstes\n"
            "Wir bemühen uns um eine hohe Verfügbarkeit, garantieren jedoch keine ununterbrochene "
            "Erreichbarkeit (Wartungsarbeiten, Störungen bei Drittanbietern wie eBay oder DHL "
            "ausgenommen). Wir dürfen den Funktionsumfang mit angemessener Ankündigung anpassen, "
            "sofern der Kernnutzen des Dienstes erhalten bleibt.\n\n"
            "§ 7 Pflichten des Kunden\n"
            "Der Kunde verpflichtet sich, ShipSync nicht missbräuchlich zu nutzen, keine "
            "rechtswidrigen Inhalte einzustellen und die Zugangsdaten zu verbundenen Konten "
            "(z. B. eBay, DHL) nur im Rahmen der jeweiligen Nutzungsbedingungen dieser Anbieter "
            "zu verwenden.\n\n"
            "§ 8 Daten des Kunden\n"
            "Der Kunde bleibt Eigentümer seiner in ShipSync eingegebenen Daten (Produkte, "
            "Bestellungen, Ausgaben). Einzelheiten zur Verarbeitung personenbezogener Daten "
            "regelt die Datenschutzerklärung (/privacy.html).\n\n"
            "§ 9 Haftung\n"
            "Wir haften unbeschränkt bei Vorsatz und grober Fahrlässigkeit sowie nach dem "
            "Produkthaftungsgesetz. Bei leicht fahrlässiger Verletzung wesentlicher "
            "Vertragspflichten (Kardinalpflichten) ist die Haftung auf den vertragstypisch "
            "vorhersehbaren Schaden begrenzt. Im Übrigen ist die Haftung ausgeschlossen.\n\n"
            "§ 10 Änderungen dieser AGB\n"
            "Änderungen dieser AGB werden dem Kunden mit angemessenem Vorlauf mitgeteilt. "
            "Widerspricht der Kunde nicht innerhalb von [PLATZHALTER: z. B. 4 Wochen], gelten "
            "die Änderungen als angenommen; hierauf wird in der Mitteilung gesondert hingewiesen.\n\n"
            "§ 11 Schlussbestimmungen\n"
            "Es gilt das Recht der Bundesrepublik Deutschland unter Ausschluss des "
            "UN-Kaufrechts. Gerichtsstand für Kaufleute ist [PLATZHALTER: Sitz des Unternehmens]. "
            "Sollten einzelne Bestimmungen unwirksam sein, bleibt die Wirksamkeit der übrigen "
            "Bestimmungen unberührt."
        ),
    },
    "withdrawal": {
        "title": "Widerrufsrecht",
        "content": (
            "Hinweis: Dies ist der gesetzliche Muster-Text (Anlage 1 und 2 zu Art. 246a § 1 Abs. 2 "
            "und 3 EGBGB) mit Platzhaltern für eure Kontaktdaten. Bitte vor Veröffentlichung durch "
            "einen Anwalt prüfen - insbesondere wenn ihr Kunden erlaubt, den Dienst sofort (vor "
            "Ablauf der 14 Tage) zu nutzen, siehe Hinweis am Ende dieser Seite.\n\n"
            "Widerrufsrecht\n\n"
            "Sie haben das Recht, binnen vierzehn Tagen ohne Angabe von Gründen diesen Vertrag "
            "zu widerrufen.\n\n"
            "Die Widerrufsfrist beträgt vierzehn Tage ab dem Tag des Vertragsschlusses.\n\n"
            "Um Ihr Widerrufsrecht auszuüben, müssen Sie uns\n"
            "[PLATZHALTER: Firmenname]\n"
            "[PLATZHALTER: Anschrift]\n"
            "Telefon: [PLATZHALTER: Telefonnummer]\n"
            "E-Mail: [PLATZHALTER: Kontakt-E-Mail]\n"
            "mittels einer eindeutigen Erklärung (z. B. ein mit der Post versandter Brief oder "
            "E-Mail) über Ihren Entschluss, diesen Vertrag zu widerrufen, informieren. Sie können "
            "dafür das unten stehende Muster-Widerrufsformular verwenden, das jedoch nicht "
            "vorgeschrieben ist.\n\n"
            "Zur Wahrung der Widerrufsfrist reicht es aus, dass Sie die Mitteilung über die "
            "Ausübung des Widerrufsrechts vor Ablauf der Widerrufsfrist absenden.\n\n"
            "Folgen des Widerrufs\n\n"
            "Wenn Sie diesen Vertrag widerrufen, haben wir Ihnen alle Zahlungen, die wir von "
            "Ihnen erhalten haben, unverzüglich und spätestens binnen vierzehn Tagen ab dem Tag "
            "zurückzuzahlen, an dem die Mitteilung über Ihren Widerruf dieses Vertrags bei uns "
            "eingegangen ist. Für diese Rückzahlung verwenden wir dasselbe Zahlungsmittel, das "
            "Sie bei der ursprünglichen Transaktion eingesetzt haben, es sei denn, mit Ihnen "
            "wurde ausdrücklich etwas anderes vereinbart; in keinem Fall werden Ihnen wegen "
            "dieser Rückzahlung Entgelte berechnet.\n\n"
            "Haben Sie verlangt, dass die Dienstleistungen während der Widerrufsfrist beginnen "
            "sollen, so haben Sie uns einen angemessenen Betrag zu zahlen, der dem Anteil der bis "
            "zu dem Zeitpunkt, zu dem Sie uns von der Ausübung des Widerrufsrechts hinsichtlich "
            "dieses Vertrags unterrichten, bereits erbrachten Dienstleistungen im Vergleich zum "
            "Gesamtumfang der im Vertrag vorgesehenen Dienstleistungen entspricht.\n\n"
            "Ende der Widerrufsbelehrung\n\n"
            "Muster-Widerrufsformular\n\n"
            "(Wenn Sie den Vertrag widerrufen wollen, dann füllen Sie bitte dieses Formular aus "
            "und senden Sie es zurück.)\n\n"
            "An [PLATZHALTER: Firmenname], [PLATZHALTER: Anschrift], E-Mail: [PLATZHALTER: "
            "Kontakt-E-Mail]:\n\n"
            "Hiermit widerrufe(n) ich/wir (*) den von mir/uns (*) abgeschlossenen Vertrag über "
            "die Erbringung der folgenden Dienstleistung: Nutzung von ShipSync\n\n"
            "Bestellt am (*):\n"
            "Name des/der Verbraucher(s):\n"
            "Anschrift des/der Verbraucher(s):\n"
            "Unterschrift des/der Verbraucher(s) (nur bei Mitteilung auf Papier):\n"
            "Datum:\n\n"
            "(*) Unzutreffendes streichen.\n\n"
            "Hinweis zum vorzeitigen Erlöschen des Widerrufsrechts: Bestätigt der Kunde bei "
            "Vertragsschluss ausdrücklich, dass der Dienst bereits vor Ablauf der Widerrufsfrist "
            "beginnen soll, und bestätigt er seine Kenntnis, dass er dadurch sein Widerrufsrecht "
            "bei vollständiger Vertragserfüllung verliert (§ 356 Abs. 4 BGB), erlischt das "
            "Widerrufsrecht entsprechend. [PLATZHALTER: Diese Bestätigung müsste aktiv beim "
            "Registrierungsformular eingeholt werden - aktuell ist das technisch noch nicht "
            "umgesetzt.]"
        ),
    },
    "contact": {
        "title": "Kontakt",
        "content": (
            "Wir helfen dir gerne weiter.\n\n"
            "E-Mail: [PLATZHALTER: Kontakt-E-Mail]\n"
            "Telefon: [PLATZHALTER: Telefonnummer]"
        ),
    },
    "faq": {
        "title": "Häufige Fragen",
        "content": (
            "Frage: Wie verbinde ich mein eBay-Konto?\n"
            "Antwort: Gehe im Dashboard auf \"Mit eBay verbinden\" und folge den Anweisungen.\n\n"
            "Frage: Wie drucke ich ein Versandetikett?\n"
            "Antwort: Öffne eine Bestellung in \"Bestellungen\" und klicke auf \"Etikett drucken\".\n\n"
            "[PLATZHALTER: Weitere häufige Fragen und Antworten hier ergänzen.]"
        ),
    },
    "impressum": {
        "title": "Impressum",
        "content": (
            "Angaben gemäß § 5 TMG\n\n"
            "[PLATZHALTER: Vor- und Nachname bzw. Firmenname]\n"
            "[PLATZHALTER: Straße und Hausnummer]\n"
            "[PLATZHALTER: PLZ und Ort]\n"
            "[PLATZHALTER: Land]\n\n"
            "Kontakt\n\n"
            "E-Mail: [PLATZHALTER: Kontakt-E-Mail]\n"
            "Telefon: [PLATZHALTER: Telefonnummer]\n\n"
            "Umsatzsteuer-ID\n\n"
            "[PLATZHALTER: Umsatzsteuer-Identifikationsnummer gemäß § 27a UStG, falls vorhanden]\n\n"
            "Verantwortlich für den Inhalt nach § 55 Abs. 2 RStV\n\n"
            "[PLATZHALTER: Name und Anschrift]\n\n"
            "Streitschlichtung\n\n"
            "Die Europäische Kommission stellt eine Plattform zur Online-Streitbeilegung (OS) "
            "bereit: https://ec.europa.eu/consumers/odr/. Wir sind nicht verpflichtet und nicht "
            "bereit, an einem Streitbeilegungsverfahren vor einer Verbraucherschlichtungsstelle "
            "teilzunehmen."
        ),
    },
    "cookies": {
        "title": "Cookie-Hinweis",
        "content": (
            "Diese Website verwendet ausschließlich technisch notwendige Speicherung im Browser, "
            "die für den Betrieb und die Sicherheit des Dienstes erforderlich ist - insbesondere "
            "zur Aufrechterhaltung deiner Anmeldesitzung und deiner Spracheinstellung.\n\n"
            "Wir setzen keine Analyse-, Tracking- oder Werbe-Cookies Dritter ein. Weitere "
            "Informationen findest du in unserer Datenschutzerklärung (/privacy.html)."
        ),
    },
    "careers": {
        "title": "Karriere",
        "content": (
            "ShipSync wächst - aktuell haben wir keine offenen Stellen ausgeschrieben, freuen "
            "uns aber jederzeit über Initiativbewerbungen.\n\n"
            "Warum bei uns arbeiten?\n\n"
            "Direkter Einfluss auf ein Produkt, das echten eBay-Verkäufern täglich Zeit spart. "
            "Kurze Entscheidungswege, keine Bürokratie. Remote-freundlich.\n\n"
            "Interesse? Schreib uns an [PLATZHALTER: Kontakt-E-Mail] - gerne mit ein paar Zeilen "
            "zu dir und was dich reizt."
        ),
    },
    "help": {
        "title": "Hilfe-Center",
        "content": (
            "Hier findest du die schnellsten Wege zu Antworten:\n\n"
            "Häufige Fragen (/faq.html) - Antworten auf die häufigsten Fragen.\n\n"
            "Systemstatus (/status.html) - aktueller Betriebsstatus von ShipSync.\n\n"
            "Änderungsprotokoll (/changelog.html) - was sich zuletzt geändert hat.\n\n"
            "Direkter E-Mail-Support: [PLATZHALTER: Kontakt-E-Mail] - für alles, was hier nicht "
            "beantwortet wird."
        ),
    },
    "status": {
        "title": "Systemstatus",
        "content": (
            "Alle Systeme funktionieren normal.\n\n"
            "Diese Seite wird manuell durch das ShipSync-Team aktualisiert. Bei größeren "
            "Störungen informieren wir hier sowie per E-Mail an betroffene Nutzer.\n\n"
            "Komponenten: API & Dashboard - Betriebsbereit. eBay-Synchronisierung - "
            "Betriebsbereit. Print-Agent-Verbindungen - Betriebsbereit.\n\n"
            "Hinweis: Der API-Server läuft im Free Tier von Render und kann nach Inaktivität "
            "einige Sekunden zum Aufwachen benötigen - das ist kein Ausfall."
        ),
    },
    "changelog": {
        "title": "Änderungsprotokoll",
        "content": (
            "[PLATZHALTER: Diese Seite über die Seitenverwaltung im Super-Admin-Bereich laufend "
            "mit den jeweils neuesten Änderungen aktualisieren.]\n\n"
            "Bald verfügbar: weitere Marktplatz-Integrationen, erweiterte "
            "Marktrecherche-Funktionen."
        ),
    },
    "integrations": {
        "title": "Integrationen",
        "content": (
            "ShipSync verbindet sich direkt mit den Werkzeugen, die du als eBay-Verkäufer "
            "bereits nutzt.\n\n"
            "Verfügbar: eBay (automatische Synchronisierung deiner Bestellungen über die "
            "offizielle eBay-API), DHL (Versandetiketten direkt aus ShipSync), lokale "
            "Etikettendrucker über den ShipSync-Print-Agent.\n\n"
            "Geplant: weitere Marktplatz- und Buchhaltungs-Integrationen. Wünsche? Schreib uns "
            "an [PLATZHALTER: Kontakt-E-Mail]."
        ),
    },
    "customers": {
        "title": "Erfolgsgeschichten",
        "content": (
            "[PLATZHALTER: Platzhalter-Inhalt - hier können echte Kundenstimmen ergänzt werden, "
            "sobald verfügbar.]\n\n"
            "\"ShipSync hat uns pro Tag mehrere Stunden manuellen Etikettendruck erspart.\" - "
            "[PLATZHALTER: Kundenname, Unternehmen]\n\n"
            "Bist du Kunde und möchtest deine Geschichte teilen? Schreib uns an "
            "[PLATZHALTER: Kontakt-E-Mail]."
        ),
    },
    "partners": {
        "title": "Partnerprogramm",
        "content": (
            "Du berätst eBay-Verkäufer oder betreibst eine Agentur im E-Commerce-Umfeld? Als "
            "ShipSync-Partner empfiehlst du unseren Dienst weiter und profitierst davon.\n\n"
            "Wie es funktioniert: Du empfiehlst ShipSync an deine Kunden oder dein Netzwerk, "
            "wir stimmen die Konditionen individuell mit dir ab.\n\n"
            "Interesse? Melde dich unter [PLATZHALTER: Kontakt-E-Mail] - wir melden uns zeitnah "
            "zurück."
        ),
    },
    "blog": {
        "title": "Blog",
        "content": (
            "[PLATZHALTER: Der Blog startet in Kürze.]\n\n"
            "Hier werden künftig Tipps rund um eBay-Verkauf, Versand und Lagerverwaltung sowie "
            "Produkt-Neuigkeiten geteilt."
        ),
    },
}


def _get_or_create_page(db: Session, slug: str) -> SitePage:
    if slug not in DEFAULT_SITE_PAGES:
        raise HTTPException(status_code=404, detail="Seite nicht gefunden")

    page = db.query(SitePage).filter(SitePage.slug == slug).first()
    if not page:
        defaults = DEFAULT_SITE_PAGES[slug]
        page = SitePage(slug=slug, title=defaults["title"], content=defaults["content"])
        db.add(page)
        db.commit()
        db.refresh(page)
    return page


@router.get("/pages/{slug}", response_model=SitePageOut)
def get_site_page(slug: str, db: Session = Depends(get_db)):
    """Deliberately no auth dependency - these are public marketing pages."""
    return _get_or_create_page(db, slug)


@router.get("/plans", response_model=list[PlanOut])
def list_active_plans(db: Session = Depends(get_db)):
    """
    Deliberately no auth dependency - a public pricing page needs to show
    this to visitors who haven't registered yet.
    """
    return (
        db.query(Plan)
        .filter(Plan.is_active == True)  # noqa: E712 - SQLAlchemy needs `== True`, not `is True`
        .order_by(Plan.display_order.asc(), Plan.created_at.asc())
        .all()
    )


@router.get("/payment-methods", response_model=list[PaymentMethodOut])
def list_active_payment_methods(db: Session = Depends(get_db)):
    """Deliberately no auth dependency - shown on the public pricing page."""
    return (
        db.query(PaymentMethod)
        .filter(PaymentMethod.is_active == True)  # noqa: E712
        .order_by(PaymentMethod.display_order.asc(), PaymentMethod.created_at.asc())
        .all()
    )


@router.get("/site-settings", response_model=SiteSettingsOut)
def get_public_site_settings(db: Session = Depends(get_db)):
    """Public logo URL - used by every page to render the current brand logo."""
    settings_row = db.query(SiteSettings).first()
    if not settings_row:
        return SiteSettingsOut(
            logo_url=None, logo_height=None, company_legal_name=None,
            company_address=None, company_tax_id=None, company_email=None,
        )
    return settings_row
