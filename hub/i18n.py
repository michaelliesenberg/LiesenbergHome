"""Sprache der Einrichtungsseite: Deutsch (Original) oder Português (Brasil).

Die Seiten werden auf Deutsch erzeugt; bei „lang = pt-BR“ übersetzt tr_html() Textknoten, Platzhalter und Titel
anhand der Tabelle unten (ganze Sätze, Leerraum egal). Unbekannte Texte bleiben deutsch.
Mitteilungen des Hubs nutzen t() mit derselben Tabelle.
"""
import html
import re

import hub_config

PT = {
    # Navigation & Rahmen
    "Übersicht": "Visão geral", "Geräte": "Dispositivos", "Energie": "Energia", "Klima": "Clima",
    "Hausgeräte": "Eletrodomésticos", "Fernzugriff": "Acesso remoto", "Personen": "Pessoas", "System": "Sistema",
    "Hub": "Hub", "Speichern": "Salvar", "Abmelden": "Sair", "Anmelden": "Entrar", "Entfernen": "Remover",
    "ändern": "alterar", "Übernehmen": "Aplicar", "Einladen": "Convidar", "Zurück": "Voltar", "Gespeichert.": "Salvo.",
    "verbunden": "conectado", "nicht angemeldet": "não conectado", "nur im Heimnetz": "só na rede de casa",
    "noch nie": "nunca", "aus": "ocultar",
    # Erster Start / Anmeldung
    "Hub einrichten": "Configurar o hub",
    "Willkommen! In drei Minuten ist dein Hub eingerichtet.": "Bem-vindo! Em três minutos seu hub estará pronto.",
    "Name deines Zuhauses": "Nome da sua casa", "z. B. Haus am See": "ex.: Casa da Praia",
    "Hub-Passwort (für diese Einrichtungsseite)": "Senha do hub (para esta página de configuração)",
    "Mindestens 8 Zeichen. Die App braucht es nicht.": "Pelo menos 8 caracteres. O app não precisa dela.",
    "Passwort wiederholen": "Repetir senha", "Los geht’s": "Começar",
    "Neuer Pi? Sicherung einspielen": "Pi novo? Restaurar backup",
    "Hast du eine Sicherung deines alten Hubs (.tgz)? Dann ist alles wieder da – Geräte, Personen, Szenen, Anmeldungen.":
        "Tem um backup do hub antigo (.tgz)? Então tudo volta – dispositivos, pessoas, cenas e logins.",
    "Sicherung einspielen": "Restaurar backup", "Hub-Passwort": "Senha do hub",
    "Passwort zu kurz oder nicht gleich.": "Senha curta demais ou diferente.",
    "Fertig! Verbinde jetzt dein iPhone (Personen) und wähle deine Geräte-Quelle.":
        "Pronto! Agora conecte seu iPhone (Pessoas) e escolha a origem dos dispositivos.",
    "Sprache": "Idioma",
    # Übersicht
    "Zuhause": "Casa", "Name des Zuhauses": "Nome da casa",
    "Ort / PLZ (für Sonnenauf- und -untergang)": "Cidade / CEP (para nascer e pôr do sol)",
    "z. B. 82335 Berg": "ex.: São Paulo ou 01310-100", "App-Adresse im Heimnetz:": "Endereço do app na rede de casa:",
    "Energie (evcc)": "Energia (evcc)", "Klima (Daikin)": "Clima (Daikin)", "Hausgeräte (Home Connect)": "Eletrodomésticos (Home Connect)",
    # Geräte-Quelle
    "Woher kommen Räume und Geräte?": "De onde vêm os cômodos e dispositivos?",
    "Shelly (WLAN, ohne Cloud)": "Shelly (Wi-Fi, sem nuvem)", "Demo-Haus (zum Ausprobieren)": "Casa demo (para experimentar)",
    "Räume umbenennen": "Renomear cômodos", "Nur für die App – an der Anlage ändert sich nichts.": "Só no app – nada muda na instalação.",
    "eigener Name": "nome próprio",
    "Die Geräte werden direkt im Heimnetz angesprochen – ohne Shelly-Cloud. Tipp: Jedem Shelly im Router eine feste Adresse geben. „aus“ blendet einen Kanal in der App aus.":
        "Os dispositivos são controlados direto na rede de casa – sem a nuvem Shelly. Dica: dê a cada Shelly um IP fixo no roteador. „ocultar“ esconde um canal no app.",
    "Noch keine Shelly-Geräte – unten suchen.": "Ainda nenhum Shelly – procure abaixo.",
    "Shelly suchen": "Procurar Shelly", "Netz (leer = automatisch)": "Rede (vazio = automático)",
    "z. B. 192.168.0.0/24": "ex.: 192.168.0.0/24", "oder eine Adresse von Hand": "ou um endereço manual",
    "z. B. 192.168.0.40": "ex.: 192.168.0.40", "Im WLAN suchen": "Procurar no Wi-Fi",
    "Name": "Nome", "Raum": "Cômodo", "Etage": "Andar", "Licht / Schalter": "Luz / interruptor", "Dimmer": "Dimmer",
    "Rollladen": "Persiana", "Gerät (nur Strom messen)": "Aparelho (só medir energia)", "Tor / Tür (Impuls)": "Portão / porta (pulso)",
    "Kein Shelly gefunden – sind Hub und Shelly im selben WLAN?": "Nenhum Shelly encontrado – o hub e o Shelly estão no mesmo Wi-Fi?",
    "Bitte eine Quelle wählen.": "Escolha uma origem.",
    # Energie
    "PV, Akku, Netz und Wallbox kommen aus": "Solar, bateria, rede e carregador vêm do", "evcc-Adresse": "Endereço do evcc",
    "Leer lassen und „Suchen“ drücken – der Hub sucht evcc im Heimnetz.": "Deixe vazio e toque em „Procurar“ – o hub procura o evcc na rede.",
    "Speichern / Suchen": "Salvar / Procurar", "Strompreise": "Preços de energia",
    "Für die Kostenübersicht in der App (Bezug, Einspeisung, gespart). Gilt für das ganze Haus – in der App können Besitzer und Vollzugriff die Preise auch ändern.":
        "Para o resumo de custos no app (consumo da rede, injeção, economia). Vale para a casa toda – no app, dono e acesso total também podem alterar.",
    "Bezugspreis (€/kWh)": "Preço da rede (R$/kWh)", "Einspeisevergütung (€/kWh)": "Crédito de injeção (R$/kWh)",
    "Grundgebühr (€/Monat, optional)": "Taxa fixa (R$/mês, opcional)",
    # Fernzugriff
    "Tailscale (empfohlen)": "Tailscale (recomendado)",
    "Damit erreicht die App dein Zuhause auch unterwegs – ohne Router-Einstellungen. Du brauchst ein kostenloses Tailscale-Konto. Beim ersten Einschalten fragt Tailscale evtl. im Browser, ob HTTPS und „Funnel“ erlaubt werden sollen – einfach bestätigen.":
        "Assim o app alcança sua casa também fora dela – sem mexer no roteador. Você precisa de uma conta Tailscale gratuita. Na primeira vez o Tailscale pode perguntar no navegador se HTTPS e „Funnel“ podem ser ativados – é só confirmar.",
    "Tailscale ist noch nicht installiert.": "O Tailscale ainda não está instalado.", "Tailscale installieren": "Instalar Tailscale",
    "Bei Tailscale anmelden": "Entrar no Tailscale", "Bei Tailscale erlauben": "Permitir no Tailscale",
    "Eigene Adresse": "Endereço próprio", "Nur falls du den Zugang schon selbst eingerichtet hast.": "Só se você já configurou o acesso por conta própria.",
    "Eigene https-Adresse (z. B. mit MyFRITZ + eigenem Zertifikat)": "Endereço https próprio (ex.: DDNS + certificado próprio)",
    "https://home.example.de": "https://casa.exemplo.com.br",
    # Personen
    "Einladung für Besitzer": "Convite para o dono",
    "Mit der iPhone-Kamera scannen – die App öffnet sich und verbindet sich. Der Code gilt 48 Stunden und nur einmal.":
        "Escaneie com a câmera do iPhone – o app abre e se conecta. O código vale 48 horas e só uma vez.",
    "Besitzer": "Dono", "Jemanden einladen": "Convidar alguém", "z. B. Laura": "ex.: Ana", "Rolle": "Papel",
    "Vollzugriff": "Acesso total", "Gast (Licht, Rollläden, Musik)": "Convidado (luz, persianas, música)",
    "Gäste sehen keine Tore/Türen und können Heizung und Klima nicht verstellen.":
        "Convidados não veem portões/portas e não podem mudar aquecimento nem clima.",
    "Einladung für": "Convite para",
    # System
    "Mitteilungen (Apple Push)": "Notificações (Apple Push)", "Key-ID (10 Zeichen)": "Key-ID (10 caracteres)",
    "Team-ID": "Team-ID", "APNs-Schlüssel (.p8)": "Chave APNs (.p8)",
    "Sicherung in Google Drive": "Backup no Google Drive",
    "Der Hub lädt jede Nacht um 3:30 eine Sicherung in deinen Google-Drive-Ordner (ältere als 120 Tage werden gelöscht). So ist alles sicher, auch wenn Pi oder SD-Karte kaputtgehen.":
        "O hub envia toda noite às 3:30 um backup para sua pasta do Google Drive (os com mais de 120 dias são apagados). Assim tudo fica seguro, mesmo se o Pi ou o cartão SD quebrar.",
    "Am Mac im Terminal einmalig:": "Uma vez, no Terminal do Mac:", "(ohne Homebrew:": "(sem Homebrew:", "Dann:": "Depois:",
    "– es öffnet sich Google, mit deinem Konto anmelden und erlauben.": "– o Google abre; entre com sua conta e permita.",
    "Im Terminal erscheint ein Text, der mit": "No Terminal aparece um texto que começa com",
    "beginnt – komplett kopieren und hier einfügen:": "– copie tudo e cole aqui:",
    "Token von rclone": "Token do rclone", "Ordner in Google Drive": "Pasta no Google Drive", "Google Drive verbinden": "Conectar Google Drive",
    "Sicherung": "Backup",
    "Enthält alles für eine Neuinstallation: Einstellungen, Zugangsdaten, Personen, Szenen, LUXOR-Projekt, Daikin/Home-Connect-Anmeldung, evcc und Caddy.":
        "Contém tudo para reinstalar: configurações, credenciais, pessoas, cenas e logins.",
    "Wie ein Schlüssel behandeln": "Trate como uma chave", "– nur privat speichern.": "– guarde só em lugar privado.",
    "Sicherung herunterladen": "Baixar backup", "Einspielen": "Restaurar", "Update": "Atualização", "Installiert:": "Instalado:",
    "· Neueste:": "· Mais nova:", "Jetzt aktualisieren": "Atualizar agora", "Neues Hub-Passwort": "Nova senha do hub",
    "Wiederholen": "Repetir", "Passwort ändern": "Alterar senha", "Fehlersuche": "Diagnóstico", "Protokoll anzeigen": "Ver registro",
    "Hub neu starten": "Reiniciar hub",
    # Klima / Hausgeräte (in Brasilien selten, trotzdem lesbar)
    "Client-ID": "Client-ID", "Client-Secret": "Client-Secret", "Code anfordern": "Pedir código", "Code aus der E-Mail": "Código do e-mail",
    "Roborock Saugroboter": "Robô aspirador Roborock", "Mammotion Mähroboter": "Robô cortador Mammotion",
    "Als Redirect-URI eintragen:": "Informe como Redirect-URI:", "Als Redirect-URI genau diese Adresse eintragen:": "Informe exatamente este endereço como Redirect-URI:",
    "Redirect-URI (nur ändern, wenn nötig)": "Redirect-URI (só altere se necessário)", "E-Mail der Roborock-App": "E-mail do app Roborock",
}

# Texte mit Zahlen/Namen
PATTERNS = [
    (r"^(\d+) Shelly gefunden, (\d+) neu übernommen\.(.*)$", lambda m: f"{m[1]} Shelly encontrado(s), {m[2]} novo(s) adicionado(s).{tr_text(m[3]) if m[3] else ''}"),
    (r"^ ?(\d+) mit Passwort-Schutz übersprungen.*$", lambda m: f" {m[1]} com senha ignorado(s) (desative o login no app Shelly)."),
    (r"^(\d+) Geräte$", lambda m: f"{m[1]} dispositivos"),
    (r"^(.+) · Kanal (\d+)$", lambda m: f"{m[1]} · canal {m[2]}"),
    (r"^Aktuell: (.+)$", lambda m: f"Atual: {m[1]}"),
    (r"^(.+) · Hub$", lambda m: f"{tr_text(m[1])} · Hub"),
    (r"^Netz ungültig: (.+)$", lambda m: f"Rede inválida: {m[1]}"),
]


def lang():
    return hub_config.get("lang") or "de"


def tr_text(t):
    key = " ".join(html.unescape(t).split())
    if not key:
        return t
    hit = PT.get(key)
    if hit is None:
        for pat, fn in PATTERNS:
            m = re.match(pat, key)
            if m:
                hit = fn(m)
                break
    if hit is None:
        return t
    lead = t[:len(t) - len(t.lstrip())]
    trail = t[len(t.rstrip()):]
    return lead + html.escape(hit, quote=False) + trail


def tr_html(page):
    if lang() != "pt-BR":
        return page
    page = page.replace("<html lang=de>", "<html lang=pt-BR>")
    head, sep, body = page.partition("</style>")          # CSS nicht anfassen
    if not sep:
        head, body = "", page
    body = re.sub(r">([^<>]+)<", lambda m: ">" + tr_text(m.group(1)) + "<", body)
    body = re.sub(r'(placeholder|title)="([^"]*)"', lambda m: f'{m.group(1)}="{tr_text(m.group(2))}"', body)
    head = re.sub(r"<title>([^<]*)</title>", lambda m: "<title>" + tr_text(m.group(1)) + "</title>", head)
    return head + sep + body


def t(text):
    """Einzelner Text (z. B. Mitteilung) in der Hub-Sprache"""
    return html.unescape(tr_text(text)) if lang() == "pt-BR" else text
