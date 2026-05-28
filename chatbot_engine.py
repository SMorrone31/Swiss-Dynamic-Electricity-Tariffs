"""
chatbot_engine.py
=================
Motore di risposta intelligente per Swiss Tariff Hub.
100% locale — nessun LLM, nessuna API esterna, nessuna dipendenza ML.

Architettura:
  1. Preprocessing  → tokenizzazione, stop-word removal, stemming leggero
  2. TF-IDF         → vettorizzazione query e knowledge base (stdlib math only)
  3. Cosine similarity → ranking intent
  4. Context window → usa ultimi N turni per disambiguare
  5. Template filling → inserisce dati live (provider, endpoint, piani)
  6. Fallback chain → risposta composita se score < soglia

Knowledge base: dizionario di "intent", ciascuno con:
  - phrases: lista di frasi trigger in 4 lingue (più frasi = più robusto)
  - answer_key: chiave nel dizionario risposte, oppure callable
  - lang_answers: risposte per lingua (opzionale, override di answer_key)
  - tags: categorie semantiche per il ranking secondario
  - priority: peso boost (1.0 = normale, 1.5 = alta priorità)
"""

from __future__ import annotations

import math
import re
import unicodedata
from collections import Counter, defaultdict
from typing import Callable, Optional

# ─────────────────────────────────────────────────────────────────────────────
#  STOP WORDS (IT + DE + FR + EN)
# ─────────────────────────────────────────────────────────────────────────────
_STOP = {
    # IT
    "il","lo","la","i","gli","le","un","uno","una","di","a","da","in","con",
    "su","per","tra","fra","e","o","ma","se","che","chi","cui","non","ho","ha",
    "hai","hanno","sono","sei","è","siamo","siete","mi","ti","si","ci","vi",
    "me","te","lui","lei","noi","voi","loro","questo","questa","questi","queste",
    "quello","quella","quelli","quelle","mio","mia","tuo","tua","suo","sua",
    "del","della","dei","degli","delle","al","alla","ai","agli","alle","dal",
    "dalla","dai","dagli","dalle","nel","nella","nei","negli","nelle","sul",
    "sulla","sui","sugli","sulle","col","coi","più","anche","già","ancora",
    "però","quindi","allora","perché","come","dove","quando","quanto","quale",
    "quali","voglio","vorrei","puoi","può","devo","avete","posso","c'è","ci sono",
    # DE
    "der","die","das","ein","eine","und","oder","aber","wenn","dass","ich",
    "du","er","sie","es","wir","ihr","sie","ist","sind","war","haben","hat",
    "hatte","werden","wird","von","mit","auf","für","an","in","zu","im","am",
    "bei","nach","aus","über","unter","vor","hinter","neben","zwischen","durch",
    "gegen","ohne","um","bis","seit","während","wegen","trotz","statt","ob",
    "wie","was","wer","wo","wann","warum","welche","welcher","welches","nicht",
    "kein","keine","mein","dein","sein","ihr","unser","euer","alle","auch",
    "noch","schon","nun","dann","hier","dort","so","sehr","mehr","wenig",
    # FR
    "le","la","les","un","une","des","de","du","au","aux","et","ou","mais",
    "si","que","qui","quoi","dont","où","je","tu","il","elle","nous","vous",
    "ils","elles","me","te","se","mon","ton","son","ma","ta","sa","notre",
    "votre","leur","mes","tes","ses","nos","vos","leurs","ce","cette","ces",
    "cet","est","sont","était","ont","avoir","être","pas","ne","plus","aussi",
    "encore","très","bien","mal","même","autre","tout","tous","toute","toutes",
    "dans","sur","sous","avec","sans","pour","par","chez","en","entre","vers",
    # EN
    "the","a","an","is","are","was","were","be","been","being","have","has",
    "had","do","does","did","will","would","could","should","may","might","shall",
    "can","of","in","to","for","on","at","by","with","from","as","or","and",
    "but","if","then","so","not","no","nor","yet","both","either","neither",
    "this","that","these","those","i","you","he","she","we","they","it","my",
    "your","his","her","our","their","its","me","him","us","them","what","which",
    "who","whom","how","when","where","why","all","any","each","few","more",
    "most","other","some","such","than","too","very","just","about","after",
    "before","between","into","through","during","above","below","up","down",
    "out","off","over","under","again","further","then","once","here","there",
}

# ─────────────────────────────────────────────────────────────────────────────
#  STEMMING LEGGERO (suffisso stripping, nessuna libreria)
# ─────────────────────────────────────────────────────────────────────────────
_STEM_SUFFIXES = [
    # IT
    "zione","zioni","amento","amenti","atura","ature","mente","zione","zioni",
    "ista","isti","ibile","ibili","ando","endo","ato","ati","ata","ate",
    "isco","isce","ano","are","ere","ire","ono","ava","evi","ivi",
    # DE
    "ierung","ungen","keit","heit","schaft","lich","lich","ung","en","er","es",
    # FR
    "ation","ations","ement","ements","ité","ités","eur","eurs","euse","euses",
    "ment","ons","ez","ant","ants","ée","ées","er","ir","re",
    # EN
    "tion","tions","ness","ment","ments","ity","ies","ing","ings","tion","ed",
    "er","ers","ly","ful","less","able","ible","al","ial","ous","ive",
]
_STEM_SUFFIXES.sort(key=len, reverse=True)

def _stem(word: str) -> str:
    """Stemming molto leggero: rimuove suffissi comuni mantenendo la radice ≥4 char."""
    if len(word) <= 5:
        return word
    for suf in _STEM_SUFFIXES:
        if word.endswith(suf) and len(word) - len(suf) >= 4:
            return word[: -len(suf)]
    return word

def _normalize_char(c: str) -> str:
    """Normalizza caratteri accentati → ASCII."""
    return unicodedata.normalize("NFKD", c).encode("ascii", "ignore").decode("ascii")

def _tokenize(text: str) -> list[str]:
    """Tokenizza, rimuove stop-word e applica stemming leggero."""
    # Normalizza accenti
    text = "".join(_normalize_char(c) for c in text.lower())
    # Estrai token alfanumerici (includi underscore per tariff_id, ecc.)
    raw = re.findall(r"[a-z0-9][a-z0-9_\-\.]*[a-z0-9]|[a-z0-9]", text)
    tokens = []
    for tok in raw:
        if tok in _STOP or len(tok) <= 1:
            continue
        tokens.append(_stem(tok))
    return tokens

# ─────────────────────────────────────────────────────────────────────────────
#  TF-IDF ENGINE
# ─────────────────────────────────────────────────────────────────────────────
class TFIDFCorpus:
    """
    Corpus TF-IDF in-memory.
    Ogni documento è una lista di tokens. I vettori IDF vengono precalcolati
    all'inizializzazione e aggiornati solo se si aggiungono nuovi documenti.
    """

    def __init__(self):
        self._docs: list[list[str]] = []
        self._doc_ids: list[str]    = []
        self._idf: dict[str, float] = {}
        self._dirty = True

    def add(self, doc_id: str, tokens: list[str]):
        self._docs.append(tokens)
        self._doc_ids.append(doc_id)
        self._dirty = True

    def _build_idf(self):
        N = len(self._docs)
        if N == 0:
            self._idf = {}
            return
        df: Counter = Counter()
        for doc in self._docs:
            df.update(set(doc))
        self._idf = {
            term: math.log((N + 1) / (cnt + 1)) + 1.0
            for term, cnt in df.items()
        }
        self._dirty = False

    def _tfidf_vec(self, tokens: list[str]) -> dict[str, float]:
        if self._dirty:
            self._build_idf()
        tf_raw: Counter = Counter(tokens)
        total = max(len(tokens), 1)
        vec: dict[str, float] = {}
        for term, cnt in tf_raw.items():
            tf = cnt / total
            idf = self._idf.get(term, math.log(2.0) + 1.0)
            vec[term] = tf * idf
        return vec

    @staticmethod
    def _cosine(v1: dict[str, float], v2: dict[str, float]) -> float:
        if not v1 or not v2:
            return 0.0
        dot   = sum(v1.get(t, 0.0) * v for t, v in v2.items())
        norm1 = math.sqrt(sum(x * x for x in v1.values()))
        norm2 = math.sqrt(sum(x * x for x in v2.values()))
        if norm1 == 0.0 or norm2 == 0.0:
            return 0.0
        return dot / (norm1 * norm2)

    def query(self, tokens: list[str], top_k: int = 3) -> list[tuple[str, float]]:
        """Ritorna i top_k doc_id con score coseno decrescente."""
        if self._dirty:
            self._build_idf()
        q_vec = self._tfidf_vec(tokens)
        scores: list[tuple[str, float]] = []
        for i, doc_tokens in enumerate(self._docs):
            d_vec = self._tfidf_vec(doc_tokens)
            score = self._cosine(q_vec, d_vec)
            if score > 0.0:
                scores.append((self._doc_ids[i], score))
        scores.sort(key=lambda x: x[1], reverse=True)
        return scores[:top_k]


# ─────────────────────────────────────────────────────────────────────────────
#  KNOWLEDGE BASE
# ─────────────────────────────────────────────────────────────────────────────
# Ogni intent ha:
#   id        : identificatore univoco
#   phrases   : lista di frasi trigger (tutte le lingue mescolate — + frasi = + robusto)
#   answers   : dict {lang: testo risposta} — ALMENO "en"
#   tags      : set di categorie semantiche
#   priority  : boost moltiplicativo al cosine score (default 1.0)
# ─────────────────────────────────────────────────────────────────────────────
_KB: list[dict] = [

    # ── GREETING ──────────────────────────────────────────────────────────────
    {
        "id": "greeting",
        "phrases": [
            "ciao buongiorno buonasera salve",
            "hello hi good morning good evening hey",
            "hallo guten tag guten morgen guten abend",
            "bonjour bonsoir salut",
            "ciao come stai come posso aiutarti",
            "hi how are you can you help me",
        ],
        "answers": {
            "it": "👋 Ciao! Sono l'assistente di **Swiss Tariff Hub**.\nPosso aiutarti con:\n- 📊 Tariffe elettriche dinamiche svizzere\n- 🔑 Registrazione e piani Free/Pro\n- 🗺️ Mappa dei provider per comune\n- 🔌 Endpoint API e formato dati\n- ⏰ Orari di aggiornamento\n\nCosa vuoi sapere?",
            "de": "👋 Hallo! Ich bin der Assistent von **Swiss Tariff Hub**.\nIch helfe Ihnen mit:\n- 📊 Dynamische Schweizer Stromtarife\n- 🔑 Registrierung und Free/Pro-Pläne\n- 🗺️ Anbieter-Karte nach Gemeinde\n- 🔌 API-Endpunkte und Datenformat\n- ⏰ Aktualisierungszeiten\n\nWie kann ich Ihnen helfen?",
            "fr": "👋 Bonjour ! Je suis l'assistant de **Swiss Tariff Hub**.\nJe peux vous aider avec :\n- 📊 Tarifs d'électricité dynamiques suisses\n- 🔑 Inscription et plans Free/Pro\n- 🗺️ Carte des fournisseurs par commune\n- 🔌 Endpoints API et format des données\n- ⏰ Horaires de mise à jour\n\nQue puis-je faire pour vous ?",
            "en": "👋 Hi! I'm the Swiss Tariff Hub assistant.\nI can help you with:\n- 📊 Swiss dynamic electricity tariffs\n- 🔑 Registration and Free/Pro plans\n- 🗺️ Provider map by municipality\n- 🔌 API endpoints and data format\n- ⏰ Update schedules\n\nWhat would you like to know?",
        },
        "tags": {"greeting", "intro"},
        "priority": 1.2,
    },

    # ── ABOUT STH ──────────────────────────────────────────────────────────────
    {
        "id": "about",
        "phrases": [
            "cos'è swiss tariff hub cosa fa come funziona descrizione presentazione",
            "what is swiss tariff hub what does it do how does it work description",
            "was ist swiss tariff hub was macht es wie funktioniert es",
            "qu'est-ce que swiss tariff hub à quoi ça sert comment ça marche",
            "servizio aggregatore tariffe elettriche svizzera EVU API REST",
            "service tariff aggregator swiss electricity utility REST API",
            "cosa offre swiss tariff hub panoramica del sistema",
            "what does swiss tariff hub offer overview of the system",
            "sth sistema tariffario svizzero aggregazione dati tariffe",
        ],
        "answers": {
            "it": "**Swiss Tariff Hub** è un servizio B2B che aggrega le tariffe elettriche dinamiche dei principali fornitori svizzeri (EVU) in un'unica **API REST normalizzata**.\n\n📊 **Dati**: slot da 15 minuti in UTC con tre componenti:\n```\nenergy_price_utc   → prezzo energia (CHF/kWh)\ngrid_price_utc     → prezzo rete (CHF/kWh)\nresidual_price_utc → componenti residue (CHF/kWh)\n```\n\n🗺️ **Dashboard**: mappa interattiva Svizzera per comune, vista Smart con semaforo, console API\n\n🌍 **4 lingue**: IT · DE · FR · EN\n\n⚡ **Provider attivi**: CKW, EKZ, EKZ Einsiedeln, Primeo, AVAG, ELAG, AEM, Groupe E",
            "de": "**Swiss Tariff Hub** ist ein B2B-Dienst, der dynamische Stromtarife der wichtigsten Schweizer Energieversorger (EVU) in einer einheitlichen **normalisierten REST-API** bündelt.\n\n📊 **Daten**: 15-Minuten-Slots in UTC mit drei Preiskomponenten:\n```\nenergy_price_utc   → Energiepreis (CHF/kWh)\ngrid_price_utc     → Netzpreis (CHF/kWh)\nresidual_price_utc → Restkomponenten (CHF/kWh)\n```\n\n🗺️ **Dashboard**: Interaktive Schweizer Karte pro Gemeinde, Smart-Ansicht, API-Konsole\n\n🌍 **4 Sprachen**: IT · DE · FR · EN\n\n⚡ **Aktive Anbieter**: CKW, EKZ, EKZ Einsiedeln, Primeo, AVAG, ELAG, AEM, Groupe E",
            "fr": "**Swiss Tariff Hub** est un service B2B qui agrège les tarifs d'électricité dynamiques des principaux fournisseurs suisses (EVU) dans une **API REST unifiée et normalisée**.\n\n📊 **Données** : créneaux de 15 minutes en UTC avec trois composantes :\n```\nenergy_price_utc   → prix énergie (CHF/kWh)\ngrid_price_utc     → prix réseau (CHF/kWh)\nresidual_price_utc → composantes résiduelles (CHF/kWh)\n```\n\n🗺️ **Dashboard** : carte interactive suisse par commune, vue Smart, console API\n\n🌍 **4 langues** : IT · DE · FR · EN\n\n⚡ **Fournisseurs actifs** : CKW, EKZ, EKZ Einsiedeln, Primeo, AVAG, ELAG, AEM, Groupe E",
            "en": "**Swiss Tariff Hub** is a B2B service that aggregates dynamic electricity tariffs from major Swiss energy utilities (EVUs) into a single **normalized REST API**.\n\n📊 **Data**: 15-minute UTC slots with three price components:\n```\nenergy_price_utc   → energy price (CHF/kWh)\ngrid_price_utc     → grid price (CHF/kWh)\nresidual_price_utc → residual components (CHF/kWh)\n```\n\n🗺️ **Dashboard**: interactive Swiss map by municipality, Smart view, API console\n\n🌍 **4 languages**: IT · DE · FR · EN\n\n⚡ **Active providers**: CKW, EKZ, EKZ Einsiedeln, Primeo, AVAG, ELAG, AEM, Groupe E",
        },
        "tags": {"about", "intro", "system"},
        "priority": 1.0,
    },

    # ── PIANI E PREZZI ────────────────────────────────────────────────────────
    {
        "id": "plans",
        "phrases": [
            "piani free pro abbonamento differenza costo quota mensile",
            "free plan pro plan subscription difference cost monthly fee",
            "free plan pro plan abonnement unterschied kosten monatlich",
            "plan gratuit plan pro abonnement différence coût mensuel",
            "quante richieste al giorno limite api free pro",
            "how many requests per day rate limit free pro",
            "upgrade piano pro da free come si passa al pro",
            "upgrade to pro how to switch from free to pro",
            "cosa include il piano pro vantaggi pro vs free",
            "what does pro plan include benefits pro vs free",
            "19 chf mensile abbonamento tariff hub",
            "chf 19 monthly subscription tariff hub",
            "tariffa mensile plan tariffhub costo mensile",
        ],
        "answers": {
            "it": "## Piani Swiss Tariff Hub\n\n**🆓 Piano Free** — gratuito\n- 500 richieste/giorno\n- Accesso a **1 tariffa** a scelta\n- Storico: solo oggi\n- Endpoint: `/api/v1/prices`, `/api/v1/tariffs`, `/api/v1/health`\n\n**⚡ Piano Pro** — CHF 19/mese\n- 2.000 richieste/giorno\n- **Tutte le tariffe** svizzere attive\n- Storico illimitato (giorni passati)\n- Tutti gli endpoint incluso `/api/v1/prices/all`\n- Fatturazione mensile, nessun vincolo\n- Disdici quando vuoi\n\n👉 Registrati su `/register` → poi attiva Pro dalla dashboard.",
            "de": "## Swiss Tariff Hub Pläne\n\n**🆓 Free-Plan** — kostenlos\n- 500 Anfragen/Tag\n- Zugang zu **1 Tarif** nach Wahl\n- Historie: nur heute\n- Endpunkte: `/api/v1/prices`, `/api/v1/tariffs`, `/api/v1/health`\n\n**⚡ Pro-Plan** — CHF 19/Monat\n- 2.000 Anfragen/Tag\n- **Alle** aktiven Schweizer Tarife\n- Unbegrenzte Historie\n- Alle Endpunkte inklusive `/api/v1/prices/all`\n- Monatliche Abrechnung, keine Bindung\n- Jederzeit kündbar\n\n👉 Registrierung unter `/register` → dann Pro-Upgrade im Dashboard.",
            "fr": "## Plans Swiss Tariff Hub\n\n**🆓 Plan Free** — gratuit\n- 500 requêtes/jour\n- Accès à **1 tarif** au choix\n- Historique : aujourd'hui seulement\n- Endpoints : `/api/v1/prices`, `/api/v1/tariffs`, `/api/v1/health`\n\n**⚡ Plan Pro** — CHF 19/mois\n- 2 000 requêtes/jour\n- **Tous les tarifs** suisses actifs\n- Historique illimité\n- Tous les endpoints dont `/api/v1/prices/all`\n- Facturation mensuelle, sans engagement\n- Résiliable à tout moment\n\n👉 Inscrivez-vous sur `/register` → puis activez Pro depuis le dashboard.",
            "en": "## Swiss Tariff Hub Plans\n\n**🆓 Free Plan** — free\n- 500 requests/day\n- Access to **1 tariff** of your choice\n- History: today only\n- Endpoints: `/api/v1/prices`, `/api/v1/tariffs`, `/api/v1/health`\n\n**⚡ Pro Plan** — CHF 19/month\n- 2,000 requests/day\n- **All** active Swiss tariffs\n- Unlimited history\n- All endpoints including `/api/v1/prices/all`\n- Monthly billing, no lock-in\n- Cancel anytime\n\n👉 Register at `/register` → then activate Pro from the dashboard.",
        },
        "tags": {"plans", "pricing", "subscription"},
        "priority": 1.1,
    },

    # ── REGISTRAZIONE ─────────────────────────────────────────────────────────
    {
        "id": "registration",
        "phrases": [
            "come mi registro registrazione come ottenere api key come iniziare accesso",
            "how do i register how to get api key how to start access sign up",
            "wie registriere ich mich wie bekomme ich api key anmeldung zugang",
            "comment s'inscrire comment obtenir clé api inscription accès",
            "processo di registrazione passi da seguire per avere una key",
            "registration process steps to follow to get a key",
            "approvazione manuale admin approva pending quanto tempo",
            "manual approval admin approves pending how long does it take",
            "ricevere email con api key dopo registrazione",
            "receive email with api key after registration",
            "form di registrazione campi richiesti nome email azienda",
            "registration form required fields name email company",
        ],
        "answers": {
            "it": "## Come registrarsi\n\n**Registrazione gratuita** in 3 passi:\n\n1. 📝 Vai su `/register` nella dashboard\n2. Compila: nome, email, azienda, paese, lingua preferita\n3. 📧 Ricevi conferma via email — poi attendi l'approvazione admin\n\nDopo l'approvazione ricevi la tua **API key** via email (formato: `stk_xxxx...`).\n\n⏱️ Approvazione tipicamente entro poche ore nei giorni lavorativi.\n\n💡 Nessuna carta di credito per il piano Free. Il Pro si attiva direttamente dalla dashboard dopo il login.",
            "de": "## Registrierung\n\n**Kostenlose Registrierung** in 3 Schritten:\n\n1. 📝 Gehen Sie zu `/register` im Dashboard\n2. Ausfüllen: Name, E-Mail, Unternehmen, Land, bevorzugte Sprache\n3. 📧 Bestätigungs-E-Mail erhalten — dann auf Admin-Genehmigung warten\n\nNach der Genehmigung erhalten Sie Ihren **API-Key** per E-Mail (Format: `stk_xxxx...`).\n\n⏱️ Genehmigung typischerweise innerhalb weniger Stunden an Werktagen.\n\n💡 Keine Kreditkarte für den Free-Plan. Pro wird direkt im Dashboard nach dem Login aktiviert.",
            "fr": "## Comment s'inscrire\n\n**Inscription gratuite** en 3 étapes :\n\n1. 📝 Allez sur `/register` dans le dashboard\n2. Remplissez : nom, e-mail, entreprise, pays, langue préférée\n3. 📧 Recevez une confirmation par e-mail — puis attendez l'approbation admin\n\nAprès approbation, vous recevrez votre **clé API** par e-mail (format : `stk_xxxx...`).\n\n⏱️ Approbation généralement dans quelques heures les jours ouvrables.\n\n💡 Aucune carte de crédit requise pour le plan Free. Le Pro s'active depuis le dashboard après connexion.",
            "en": "## How to Register\n\n**Free registration** in 3 steps:\n\n1. 📝 Go to `/register` in the dashboard\n2. Fill in: name, email, company, country, preferred language\n3. 📧 Receive confirmation email — then wait for admin approval\n\nAfter approval you receive your **API key** by email (format: `stk_xxxx...`).\n\n⏱️ Approval typically within a few hours on business days.\n\n💡 No credit card required for the Free plan. Pro is activated directly from the dashboard after login.",
        },
        "tags": {"registration", "api_key", "access"},
        "priority": 1.1,
    },

    # ── LOGIN / AUTENTICAZIONE ────────────────────────────────────────────────
    {
        "id": "login",
        "phrases": [
            "come mi loggo login accesso inserire api key dove metto la chiave",
            "how do i log in login access insert api key where do i enter the key",
            "wie logge ich mich ein anmelden eingabe api key wo gebe ich den key ein",
            "comment se connecter login saisir clé api où mettre la clé",
            "fare il login dalla dashboard tab api inserire key",
            "log in from dashboard api tab enter key",
            "sessione scaduta devo rifare il login token sessione",
            "session expired need to log in again session token",
            "ho perso la mia api key come la recupero",
            "i lost my api key how do i recover it",
            "api key smarrita recupero chiave",
            "lost api key recovery",
        ],
        "answers": {
            "it": "## Come fare il login\n\n1. Vai al tab **API** nella dashboard\n2. Inserisci la tua API key nel campo apposito (formato `stk_xxxx...`)\n3. Clicca **Accedi** — il tuo profilo comparirà nell'header\n\n🔒 La sessione è temporanea (token JWT): se chiudi il browser dovrai ri-loggarti.\n\n**API key smarrita?**\nContatta **support@tariffhub.ch** indicando nome e email di registrazione — ti invieremo una nuova chiave dopo verifica.\n\n💡 Se hai già una key attiva puoi richiederne una sostituzione dal form `/register` (seleziona \"Sostituisci key esistente\").",
            "de": "## Login-Vorgang\n\n1. Gehen Sie zum Tab **API** im Dashboard\n2. Geben Sie Ihren API-Key ein (Format `stk_xxxx...`)\n3. Klicken Sie auf **Anmelden** — Ihr Profil erscheint im Header\n\n🔒 Die Sitzung ist temporär (JWT-Token): Beim Schließen des Browsers müssen Sie sich erneut anmelden.\n\n**API-Key verloren?**\nKontaktieren Sie **support@tariffhub.ch** mit Name und Registrierungs-E-Mail — wir senden nach Prüfung einen neuen Key.\n\n💡 Mit einer aktiven Key können Sie über das Formular `/register` eine Ersetzung anfordern.",
            "fr": "## Comment se connecter\n\n1. Allez à l'onglet **API** du dashboard\n2. Saisissez votre clé API (format `stk_xxxx...`)\n3. Cliquez sur **Se connecter** — votre profil apparaît dans l'en-tête\n\n🔒 La session est temporaire (token JWT) : en fermant le navigateur vous devrez vous reconnecter.\n\n**Clé API perdue ?**\nContactez **support@tariffhub.ch** avec votre nom et e-mail d'inscription — nous enverrons une nouvelle clé après vérification.\n\n💡 Avec une clé active, vous pouvez demander un remplacement via `/register`.",
            "en": "## How to Log In\n\n1. Go to the **API** tab in the dashboard\n2. Enter your API key (format `stk_xxxx...`)\n3. Click **Log in** — your profile appears in the header\n\n🔒 Session is temporary (JWT token): closing the browser requires re-login.\n\n**Lost your API key?**\nContact **support@tariffhub.ch** with your name and registration email — we'll send a new key after verification.\n\n💡 With an active key you can request a replacement via `/register`.",
        },
        "tags": {"login", "auth", "api_key", "session"},
        "priority": 1.1,
    },

    # ── ENDPOINT API ──────────────────────────────────────────────────────────
    {
        "id": "endpoints",
        "phrases": [
            "endpoint api disponibili lista endpoint come chiamare l'api",
            "available api endpoints list how to call the api",
            "verfügbare api endpunkte liste wie api aufrufen",
            "endpoints api disponibles liste comment appeler l'api",
            "get prezzi oggi domani tariff_id start_time end_time",
            "get prices today tomorrow tariff_id start_time end_time",
            "api v1 prices today tomorrow all tariffs health",
            "/api/v1/prices /api/v1/tariffs /api/v1/health endpoint",
            "curl esempio richiesta http come usare l'api con curl",
            "curl example http request how to use the api with curl",
            "bearer token authorization header autenticazione api",
            "bearer token authorization header api authentication",
            "parametri endpoint tariff_id data start_time",
            "endpoint parameters tariff_id date start_time",
        ],
        "answers": {
            "it": "## Endpoint API disponibili\n\n| Metodo | Path | Descrizione |\n|--------|------|-------------|\n| GET | `/api/v1/tariffs` | Lista tariffe disponibili |\n| GET | `/api/v1/prices?tariff_id=X&start_time=Y` | Prezzi (range libero) |\n| GET | `/api/v1/prices/today?tariff_id=X` | 96 slot di oggi |\n| GET | `/api/v1/prices/tomorrow?tariff_id=X` | Prezzi domani (dopo ~17:30 UTC) |\n| GET | `/api/v1/prices/all` | Tutte le tariffe, tutti gli slot (🔒 Pro) |\n| GET | `/api/v1/health` | Stato sistema e uptime |\n\n**Header richiesto:**\n```\nAuthorization: Bearer stk_xxxxxxxxxxxx\n```\n\n**Esempio curl:**\n```bash\ncurl -H \"Authorization: Bearer stk_xxx\" \\\n  \"/api/v1/prices/today?tariff_id=ckw_home_dynamic\"\n```",
            "de": "## Verfügbare API-Endpunkte\n\n| Methode | Pfad | Beschreibung |\n|---------|------|--------------|\n| GET | `/api/v1/tariffs` | Liste verfügbarer Tarife |\n| GET | `/api/v1/prices?tariff_id=X&start_time=Y` | Preise (freier Bereich) |\n| GET | `/api/v1/prices/today?tariff_id=X` | 96 Slots für heute |\n| GET | `/api/v1/prices/tomorrow?tariff_id=X` | Preise morgen (nach ~17:30 UTC) |\n| GET | `/api/v1/prices/all` | Alle Tarife, alle Slots (🔒 Pro) |\n| GET | `/api/v1/health` | Systemstatus und Uptime |\n\n**Erforderlicher Header:**\n```\nAuthorization: Bearer stk_xxxxxxxxxxxx\n```",
            "fr": "## Endpoints API disponibles\n\n| Méthode | Chemin | Description |\n|---------|--------|-------------|\n| GET | `/api/v1/tariffs` | Liste des tarifs disponibles |\n| GET | `/api/v1/prices?tariff_id=X&start_time=Y` | Prix (plage libre) |\n| GET | `/api/v1/prices/today?tariff_id=X` | 96 créneaux d'aujourd'hui |\n| GET | `/api/v1/prices/tomorrow?tariff_id=X` | Prix de demain (après ~17:30 UTC) |\n| GET | `/api/v1/prices/all` | Tous les tarifs, tous les créneaux (🔒 Pro) |\n| GET | `/api/v1/health` | État du système et uptime |\n\n**En-tête requis :**\n```\nAuthorization: Bearer stk_xxxxxxxxxxxx\n```",
            "en": "## Available API Endpoints\n\n| Method | Path | Description |\n|--------|------|-------------|\n| GET | `/api/v1/tariffs` | List available tariffs |\n| GET | `/api/v1/prices?tariff_id=X&start_time=Y` | Prices (free range) |\n| GET | `/api/v1/prices/today?tariff_id=X` | 96 slots for today |\n| GET | `/api/v1/prices/tomorrow?tariff_id=X` | Tomorrow's prices (after ~17:30 UTC) |\n| GET | `/api/v1/prices/all` | All tariffs, all slots (🔒 Pro) |\n| GET | `/api/v1/health` | System health and uptime |\n\n**Required header:**\n```\nAuthorization: Bearer stk_xxxxxxxxxxxx\n```\n\n**Example curl:**\n```bash\ncurl -H \"Authorization: Bearer stk_xxx\" \\\n  \"/api/v1/prices/today?tariff_id=ckw_home_dynamic\"\n```",
        },
        "tags": {"api", "endpoints", "technical"},
        "priority": 1.0,
    },

    # ── FORMATO DATI ──────────────────────────────────────────────────────────
    {
        "id": "data_format",
        "phrases": [
            "formato dati api risposta json slot 15 minuti struttura",
            "api data format json response slot 15 minutes structure",
            "api datenformat json antwort 15-minuten-slots struktur",
            "format données api réponse json créneaux 15 minutes structure",
            "energy_price_utc grid_price_utc residual_price_utc cosa significa",
            "energy_price_utc grid_price_utc residual_price_utc meaning",
            "come interpretare i dati prezzi energia rete residuo",
            "how to interpret price data energy grid residual",
            "chf kwh unità di misura prezzi tariffa",
            "chf kwh unit of measurement tariff prices",
            "timestamp utc iso 8601 formato data ora slot",
            "timestamp utc iso 8601 datetime format slot",
            "rp kwh conversione centesimi franchi prezzi",
            "rp kwh conversion cents francs prices",
        ],
        "answers": {
            "it": "## Formato dati API\n\nOgni risposta contiene **slot da 15 minuti in UTC**:\n\n```json\n[\n  {\n    \"2025-01-15T00:00:00Z\": {\n      \"energy_price_utc\": 0.08234,\n      \"grid_price_utc\":   0.05610,\n      \"residual_price_utc\": 0.01200\n    }\n  },\n  ...\n]\n```\n\n| Campo | Descrizione | Unità |\n|-------|-------------|-------|\n| `energy_price_utc` | Prezzo energia elettrica | CHF/kWh |\n| `grid_price_utc` | Tariffa di rete (distribuzione) | CHF/kWh |\n| `residual_price_utc` | Tasse, SDL, Stromreserve, ecc. | CHF/kWh |\n\n💡 **Totale**: somma dei tre campi × 100 = Rp/kWh\n\n⚙️ **Giorno normale**: 96 slot · **Primavera DST**: 92 · **Autunno DST**: 100",
            "de": "## API-Datenformat\n\nJede Antwort enthält **15-Minuten-Slots in UTC**:\n\n```json\n[\n  {\n    \"2025-01-15T00:00:00Z\": {\n      \"energy_price_utc\": 0.08234,\n      \"grid_price_utc\":   0.05610,\n      \"residual_price_utc\": 0.01200\n    }\n  }\n]\n```\n\n| Feld | Beschreibung | Einheit |\n|------|-------------|--------|\n| `energy_price_utc` | Strompreis | CHF/kWh |\n| `grid_price_utc` | Netzentgelt | CHF/kWh |\n| `residual_price_utc` | Steuern, SDL, Stromreserve usw. | CHF/kWh |\n\n💡 **Gesamt**: Summe × 100 = Rp/kWh",
            "fr": "## Format des données API\n\nChaque réponse contient des **créneaux de 15 minutes en UTC** :\n\n```json\n[\n  {\n    \"2025-01-15T00:00:00Z\": {\n      \"energy_price_utc\": 0.08234,\n      \"grid_price_utc\":   0.05610,\n      \"residual_price_utc\": 0.01200\n    }\n  }\n]\n```\n\n| Champ | Description | Unité |\n|-------|-------------|-------|\n| `energy_price_utc` | Prix de l'énergie | CHF/kWh |\n| `grid_price_utc` | Tarif réseau | CHF/kWh |\n| `residual_price_utc` | Taxes, SDL, etc. | CHF/kWh |\n\n💡 **Total** : somme × 100 = Rp/kWh",
            "en": "## API Data Format\n\nEach response contains **15-minute UTC slots**:\n\n```json\n[\n  {\n    \"2025-01-15T00:00:00Z\": {\n      \"energy_price_utc\": 0.08234,\n      \"grid_price_utc\":   0.05610,\n      \"residual_price_utc\": 0.01200\n    }\n  }\n]\n```\n\n| Field | Description | Unit |\n|-------|-------------|------|\n| `energy_price_utc` | Electricity price | CHF/kWh |\n| `grid_price_utc` | Grid distribution fee | CHF/kWh |\n| `residual_price_utc` | Taxes, SDL, Stromreserve, etc. | CHF/kWh |\n\n💡 **Total**: sum of three fields × 100 = Rp/kWh\n\n⚙️ **Normal day**: 96 slots · **Spring DST**: 92 · **Autumn DST**: 100",
        },
        "tags": {"data_format", "technical", "api"},
        "priority": 1.0,
    },

    # ── ORARI AGGIORNAMENTO ───────────────────────────────────────────────────
    {
        "id": "update_times",
        "phrases": [
            "quando vengono aggiornati i dati orario aggiornamento prezzi domani",
            "when is data updated update schedule prices tomorrow",
            "wann werden daten aktualisiert aktualisierungszeit preise morgen",
            "quand les données sont mises à jour horaire mise à jour prix demain",
            "ckw aggiornamento 11:05 utc provider 17:30 utc fetch scheduler",
            "ckw update 11:05 utc provider 17:30 utc fetch scheduler",
            "perché i prezzi di domani non ci sono ancora quando arrivano",
            "why are tomorrow's prices not available yet when will they arrive",
            "prezzi del giorno successivo day ahead disponibilità",
            "next day prices day ahead availability",
            "dati non aggiornati prezzi mancanti quando arriva l'aggiornamento",
            "data not updated missing prices when does update arrive",
        ],
        "answers": {
            "it": "## Orari di aggiornamento\n\n| Provider | Orario fetch (UTC) | Note |\n|----------|--------------------|------|\n| **CKW** | 11:05 UTC | Day-ahead, giorno +1 |\n| **EKZ, Primeo, AVAG, ELAG, AEM, Groupe E, EKZ Einsiedeln** | 17:30 UTC | Day-ahead |\n\n⏰ I prezzi del giorno **successivo** sono tipicamente disponibili dopo le **17:30 UTC** (19:30 ora svizzera).\n\n🔄 Se un provider non risponde, il sistema riprova automaticamente. Dopo 3 fallimenti consecutivi il fetcher viene sospeso e riattivato al giorno successivo.\n\n📊 Puoi controllare lo stato live su `/api/v1/health`.",
            "de": "## Aktualisierungszeiten\n\n| Anbieter | Abrufzeit (UTC) | Hinweise |\n|----------|-----------------|----------|\n| **CKW** | 11:05 UTC | Day-Ahead, Tag +1 |\n| **EKZ, Primeo, AVAG, ELAG, AEM, Groupe E, EKZ Einsiedeln** | 17:30 UTC | Day-Ahead |\n\n⏰ Preise für den **nächsten Tag** sind typischerweise nach **17:30 UTC** (19:30 Schweizer Zeit) verfügbar.\n\n🔄 Bei fehlgeschlagenen Abrufen wird automatisch wiederholt. Nach 3 aufeinanderfolgenden Fehlern wird der Abruf bis zum nächsten Tag ausgesetzt.\n\n📊 Live-Status unter `/api/v1/health` prüfbar.",
            "fr": "## Horaires de mise à jour\n\n| Fournisseur | Heure (UTC) | Notes |\n|-------------|-------------|-------|\n| **CKW** | 11:05 UTC | Day-ahead, jour +1 |\n| **EKZ, Primeo, AVAG, ELAG, AEM, Groupe E, EKZ Einsiedeln** | 17:30 UTC | Day-ahead |\n\n⏰ Les prix du **lendemain** sont typiquement disponibles après **17:30 UTC** (19:30 heure suisse).\n\n🔄 En cas d'échec, le système réessaie automatiquement. Après 3 échecs consécutifs, le fetch est suspendu jusqu'au lendemain.\n\n📊 État en direct sur `/api/v1/health`.",
            "en": "## Update Schedule\n\n| Provider | Fetch time (UTC) | Notes |\n|----------|-----------------|-------|\n| **CKW** | 11:05 UTC | Day-ahead, day +1 |\n| **EKZ, Primeo, AVAG, ELAG, AEM, Groupe E, EKZ Einsiedeln** | 17:30 UTC | Day-ahead |\n\n⏰ Next-day prices are typically available after **17:30 UTC** (19:30 Swiss time).\n\n🔄 If a provider fails to respond, the system retries automatically. After 3 consecutive failures the fetcher is suspended until the next day.\n\n📊 Check live status at `/api/v1/health`.",
        },
        "tags": {"schedule", "update", "technical"},
        "priority": 1.0,
    },

    # ── DST / ORA LEGALE ──────────────────────────────────────────────────────
    {
        "id": "dst",
        "phrases": [
            "ora legale dst cambio ora daylight saving time 92 96 100 slot",
            "daylight saving time dst time change 92 96 100 slots",
            "sommerzeit dst zeitumstellung 92 96 100 slots",
            "heure d'été dst changement d'heure 92 96 100 créneaux",
            "primavera perdita ora 92 slot autunno ora in più 100 slot",
            "spring clock forward 92 slots autumn clock back 100 slots",
            "europe zurich timezone fuso orario svizzera",
            "europe zurich timezone switzerland time zone",
            "quanti slot ci sono in un giorno 96 slot 15 minuti",
            "how many slots in a day 96 slots 15 minutes",
        ],
        "answers": {
            "it": "## Gestione ora legale (DST)\n\nIl sistema usa il fuso orario `Europe/Zurich` per determinare il numero corretto di slot:\n\n| Tipo di giorno | Slot | Ore | Quando |\n|----------------|------|-----|--------|\n| **Giorno normale** | **96** | 24h | Tutto l'anno tranne transizioni |\n| **Domenica cambio ora (primavera)** | **92** | 23h | Ultima domenica di marzo (−1h) |\n| **Domenica cambio ora (autunno)** | **100** | 25h | Ultima domenica di ottobre (+1h) |\n\nEvery slot = 15 minuti UTC. Il sistema gestisce le transizioni automaticamente.",
            "de": "## Sommerzeit-Behandlung (DST)\n\nDas System verwendet die Zeitzone `Europe/Zurich`:\n\n| Tagestyp | Slots | Stunden | Wann |\n|----------|-------|---------|------|\n| **Normaler Tag** | **96** | 24h | Das ganze Jahr außer Übergängen |\n| **Frühjahrszeitumstellung** | **92** | 23h | Letzter Sonntag im März (−1h) |\n| **Herbstzeitumstellung** | **100** | 25h | Letzter Sonntag im Oktober (+1h) |\n\nJeder Slot = 15 Minuten UTC. Das System verarbeitet Übergänge automatisch.",
            "fr": "## Gestion du changement d'heure (DST)\n\nLe système utilise le fuseau horaire `Europe/Zurich` :\n\n| Type de jour | Créneaux | Heures | Quand |\n|-------------|----------|--------|-------|\n| **Jour normal** | **96** | 24h | Toute l'année sauf transitions |\n| **Passage à l'heure d'été** | **92** | 23h | Dernier dimanche de mars (−1h) |\n| **Passage à l'heure d'hiver** | **100** | 25h | Dernier dimanche d'octobre (+1h) |\n\nChaque créneau = 15 minutes UTC.",
            "en": "## Daylight Saving Time (DST) Handling\n\nThe system uses `Europe/Zurich` timezone:\n\n| Day type | Slots | Hours | When |\n|----------|-------|-------|------|\n| **Normal day** | **96** | 24h | All year except transitions |\n| **Spring forward** | **92** | 23h | Last Sunday of March (−1h) |\n| **Autumn back** | **100** | 25h | Last Sunday of October (+1h) |\n\nEach slot = 15 minutes UTC. The system handles transitions automatically.",
        },
        "tags": {"dst", "timezone", "technical"},
        "priority": 1.0,
    },

    # ── MAPPA ─────────────────────────────────────────────────────────────────
    {
        "id": "map",
        "phrases": [
            "mappa come funziona mappa svizzera comuni provider mappa interattiva",
            "map how does map work swiss map municipalities provider interactive map",
            "karte wie funktioniert karte schweizer karte gemeinden anbieter",
            "carte comment fonctionne carte suisse communes fournisseurs",
            "cerco il mio comune mappa zip copertura tariffa dinamica",
            "find my municipality map zip coverage dynamic tariff",
            "quali comuni sono coperti copertura geografica provider",
            "which municipalities are covered geographic coverage provider",
            "cliccare comune vedere prezzi mappa pannello prezzi",
            "click municipality view prices map price panel",
            "colori mappa cosa significano legenda mappa provider",
            "map colors what do they mean map legend providers",
            "zoom panoramica mappa scorrimento mappa",
            "zoom pan map scroll map",
            "mappa non carica problema mappa svizzera topoJSON d3",
            "map not loading problem swiss map topojson d3",
        ],
        "answers": {
            "it": "## Mappa interattiva dei provider\n\nLa mappa mostra la **copertura delle tariffe dinamiche** per ogni comune svizzero.\n\n🎨 **Colori per provider:**\n- Ogni colore rappresenta un fornitore (CKW, EKZ, Groupe E, ecc.)\n- I comuni grigi non hanno tariffe dinamiche disponibili nel 2026\n\n🖱️ **Come usarla:**\n1. **Hover** su un comune → tooltip con nome e provider\n2. **Click** su un comune → pannello laterale con:\n   - Grafico a barre 96 slot (verde=economico, rosso=caro, blu=slot corrente)\n   - Statistiche: min, media, max, prezzo attuale\n3. **Scroll/pinch** → zoom · Trascina → pan · Tasto Reset → vista iniziale\n4. **Filtri** in alto → mostra solo i comuni di un provider\n\n📍 **Copertura**: 457 su 2.148 comuni svizzeri (fonte: ElCom 2026)",
            "de": "## Interaktive Anbieter-Karte\n\nDie Karte zeigt die **Abdeckung dynamischer Tarife** für jede Schweizer Gemeinde.\n\n🎨 **Farben nach Anbieter:**\n- Jede Farbe steht für einen Anbieter (CKW, EKZ, Groupe E usw.)\n- Graue Gemeinden haben 2026 keine dynamischen Tarife\n\n🖱️ **Bedienung:**\n1. **Hover** über Gemeinde → Tooltip mit Name und Anbieter\n2. **Klick** auf Gemeinde → Seitenbereich mit:\n   - 96-Slot-Balkendiagramm (grün=günstig, rot=teuer, blau=aktueller Slot)\n   - Statistiken: Min, Durchschnitt, Max, aktueller Preis\n3. **Scroll/Pinch** → Zoom · Ziehen → Pan · Reset-Taste → Anfangsansicht\n4. **Filter** oben → nur Gemeinden eines Anbieters anzeigen\n\n📍 **Abdeckung**: 457 von 2.148 Schweizer Gemeinden (Quelle: ElCom 2026)",
            "fr": "## Carte interactive des fournisseurs\n\nLa carte montre la **couverture des tarifs dynamiques** pour chaque commune suisse.\n\n🎨 **Couleurs par fournisseur :**\n- Chaque couleur représente un fournisseur (CKW, EKZ, Groupe E, etc.)\n- Les communes grises n'ont pas de tarifs dynamiques disponibles en 2026\n\n🖱️ **Utilisation :**\n1. **Survol** d'une commune → info-bulle avec nom et fournisseur\n2. **Clic** sur une commune → panneau latéral avec :\n   - Graphique en barres 96 créneaux (vert=pas cher, rouge=cher, bleu=créneau actuel)\n   - Statistiques : min, moyenne, max, prix actuel\n3. **Scroll/pinch** → zoom · Glisser → panoramique · Bouton Reset → vue initiale\n4. **Filtres** en haut → afficher seulement les communes d'un fournisseur\n\n📍 **Couverture** : 457 sur 2 148 communes suisses (source : ElCom 2026)",
            "en": "## Interactive Provider Map\n\nThe map shows **dynamic tariff coverage** for every Swiss municipality.\n\n🎨 **Colors by provider:**\n- Each color represents a provider (CKW, EKZ, Groupe E, etc.)\n- Grey municipalities have no dynamic tariffs available in 2026\n\n🖱️ **How to use it:**\n1. **Hover** over a municipality → tooltip with name and provider\n2. **Click** on a municipality → side panel with:\n   - 96-slot bar chart (green=cheap, red=expensive, blue=current slot)\n   - Statistics: min, average, max, current price\n3. **Scroll/pinch** → zoom · Drag → pan · Reset button → initial view\n4. **Filters** at top → show only one provider's municipalities\n\n📍 **Coverage**: 457 out of 2,148 Swiss municipalities (source: ElCom 2026)",
        },
        "tags": {"map", "ui", "dashboard"},
        "priority": 1.0,
    },

    # ── VISTA SMART ───────────────────────────────────────────────────────────
    {
        "id": "smart_view",
        "phrases": [
            "vista smart come funziona semaforo segnale miglior ora",
            "smart view how does it work traffic light signal best time",
            "smart-ansicht wie funktioniert ampel signal beste stunden",
            "vue smart comment fonctionne feu signal meilleures heures",
            "semaforo verde giallo rosso prezzo corrente ora più economica",
            "traffic light green yellow red current price cheapest time",
            "migliori orari per consumare energia ora di punta offpeak",
            "best times to consume energy peak off-peak hours",
            "smart tab tariffa confronto multiple tariffe",
            "smart tab tariff comparison multiple tariffs",
            "come leggere il segnale semaforo smart",
            "how to read the smart signal traffic light",
        ],
        "answers": {
            "it": "## Vista Smart\n\nLa vista Smart mostra in tempo reale il **segnale di convenienza** per la tariffa selezionata.\n\n🚦 **Semaforo prezzi:**\n| Colore | Significato | Quando |\n|--------|-------------|--------|\n| 🟢 Verde | Prezzo basso — buon momento per consumare | < 85% della media giornaliera |\n| 🟡 Giallo | Prezzo medio | 85–115% della media |\n| 🔴 Rosso | Prezzo alto — meglio attendere | > 115% della media |\n\n⏰ **Best times**: mostra i 3 slot più economici delle prossime 4 ore\n\n⚡ **Prezzo attuale**: Rp/kWh totale (energia + rete + residuo)\n\n📊 **Profilo giornaliero**: grafico completo 96 slot con tooltip interattivo\n\n🔒 Il profilo dettagliato e i best times richiedono il login con API key.",
            "de": "## Smart-Ansicht\n\nDie Smart-Ansicht zeigt in Echtzeit das **Preis-Signal** für den gewählten Tarif.\n\n🚦 **Preisampel:**\n| Farbe | Bedeutung | Wann |\n|-------|-----------|------|\n| 🟢 Grün | Niedriger Preis — guter Zeitpunkt zum Verbrauch | < 85% des Tagesdurchschnitts |\n| 🟡 Gelb | Mittlerer Preis | 85–115% des Durchschnitts |\n| 🔴 Rot | Hoher Preis — besser warten | > 115% des Durchschnitts |\n\n⏰ **Beste Zeiten**: die 3 günstigsten Slots der nächsten 4 Stunden",
            "fr": "## Vue Smart\n\nLa vue Smart affiche en temps réel le **signal de prix** pour le tarif sélectionné.\n\n🚦 **Feu de prix :**\n| Couleur | Signification | Quand |\n|---------|---------------|-------|\n| 🟢 Vert | Prix bas — bon moment pour consommer | < 85% de la moyenne journalière |\n| 🟡 Jaune | Prix moyen | 85–115% de la moyenne |\n| 🔴 Rouge | Prix élevé — mieux vaut attendre | > 115% de la moyenne |\n\n⏰ **Meilleures heures** : les 3 créneaux les moins chers dans les 4 prochaines heures",
            "en": "## Smart View\n\nThe Smart view shows the real-time **price signal** for the selected tariff.\n\n🚦 **Price traffic light:**\n| Color | Meaning | When |\n|-------|---------|------|\n| 🟢 Green | Low price — good time to consume | < 85% of daily average |\n| 🟡 Yellow | Average price | 85–115% of average |\n| 🔴 Red | High price — better to wait | > 115% of average |\n\n⏰ **Best times**: shows the 3 cheapest slots in the next 4 hours\n\n⚡ **Current price**: total Rp/kWh (energy + grid + residual)\n\n📊 **Daily profile**: full 96-slot chart with interactive tooltip\n\n🔒 Detailed profile and best times require login with API key.",
        },
        "tags": {"smart", "ui", "dashboard", "signal"},
        "priority": 1.0,
    },

    # ── VISTA DATA / DEVELOPER ────────────────────────────────────────────────
    {
        "id": "data_view",
        "phrases": [
            "vista data developer tab dati grafico come funziona",
            "data view developer tab chart how does it work",
            "data ansicht developer tab diagramm wie funktioniert es",
            "vue data onglet développeur graphique comment ça marche",
            "selezionare tariffa tab data grafico prezzi giorni",
            "select tariff data tab price chart days",
            "day buttons oggi ieri giorni passati storico",
            "day buttons today yesterday past days history",
            "slot inspector 15 minuti dettaglio slot hovering chart",
            "slot inspector 15 minutes slot detail hovering chart",
            "accesso storico giorni passati pro free guest lock",
            "historical access past days pro free guest locked",
            "inspector pannello slot prezzo corrente percentuale",
            "inspector slot panel current price percentage",
        ],
        "answers": {
            "it": "## Vista Data (Developer)\n\nLa vista Data permette di esplorare i prezzi giornalieri in dettaglio.\n\n📅 **Selezione giorno**: bottoni pill per gli ultimi 5 giorni con dati\n- Il pulsante \"oggi\" ha bordo colorato\n- I giorni passati richiedono login (Free/Pro); per i guest è solo oggi\n\n📊 **Grafico**: linee per energia, rete, residuo, totale (Rp/kWh)\n- Hovering → inspector laterale con dettaglio slot 15min\n- Crosshair sul grafico (solo prima tariffa per guest)\n\n🔍 **Slot inspector** (pannello destro):\n- Prezzo totale corrente in Rp/kWh\n- Breakdown: energia / rete / residuo\n- % rispetto alla media giornaliera\n- Ora UTC e ora locale Svizzera\n\n📈 **Monthly summary**: griglia mensile con min/media/max per giorno\n\n🔒 **Accesso:**\n- Guest: prime 2 tariffe, solo oggi\n- Free: tutte le tariffe, ma solo 1 a scelta con storico\n- Pro: tutto sbloccato",
            "de": "## Daten-Ansicht (Developer)\n\nDie Daten-Ansicht ermöglicht detaillierte Preiserkundung.\n\n📅 **Tagesauswahl**: Pill-Buttons für die letzten 5 Tage mit Daten\n- \"Heute\"-Button hat farbigen Rand\n- Vergangene Tage erfordern Login; Gäste sehen nur heute\n\n📊 **Diagramm**: Linien für Energie, Netz, Restwert, Gesamt (Rp/kWh)\n\n🔍 **Slot-Inspector**: Detailansicht für jeden 15-Minuten-Slot",
            "fr": "## Vue Data (Developer)\n\nLa vue Data permet d'explorer les prix journaliers en détail.\n\n📅 **Sélection du jour** : boutons pill pour les 5 derniers jours avec données\n- Le bouton \"aujourd'hui\" a une bordure colorée\n- Les jours passés nécessitent une connexion\n\n📊 **Graphique** : lignes énergie, réseau, résiduel, total (Rp/kWh)\n\n🔍 **Inspecteur de créneau** : détail pour chaque créneau de 15 minutes",
            "en": "## Data View (Developer)\n\nThe Data view allows you to explore daily prices in detail.\n\n📅 **Day selection**: pill buttons for the last 5 days with data\n- \"Today\" button has colored border\n- Past days require login (Free/Pro); guests see today only\n\n📊 **Chart**: lines for energy, grid, residual, total (Rp/kWh)\n- Hover → side inspector with 15-min slot detail\n- Crosshair on chart (only first tariff for guests)\n\n🔍 **Slot inspector** (right panel):\n- Current total price in Rp/kWh\n- Breakdown: energy / grid / residual\n- % vs daily average\n- UTC time and Swiss local time\n\n📈 **Monthly summary**: monthly grid with min/avg/max per day\n\n🔒 **Access:**\n- Guest: first 2 tariffs, today only\n- Free: all tariffs, but only 1 chosen tariff with history\n- Pro: everything unlocked",
        },
        "tags": {"data_view", "ui", "dashboard", "chart"},
        "priority": 1.0,
    },

    # ── SICUREZZA ─────────────────────────────────────────────────────────────
    {
        "id": "security",
        "phrases": [
            "sicurezza autenticazione bearer token header sha256 hashing",
            "security authentication bearer token header sha256 hashing",
            "sicherheit authentifizierung bearer token header sha256",
            "sécurité authentification bearer token en-tête sha256",
            "come proteggere la mia api key sicurezza chiave",
            "how to protect my api key key security",
            "dati sensibili memorizzati in chiaro database sicuro",
            "sensitive data stored in plaintext secure database",
            "https ssl crittografia traffico api",
            "https ssl encryption api traffic",
        ],
        "answers": {
            "it": "## Sicurezza\n\n🔑 **Autenticazione**: ogni richiesta API deve includere la key nell'header:\n```\nAuthorization: Bearer stk_xxxxxxxxxxxx\n```\n\n🔒 **Hashing**: le API key sono hashate con **SHA-256** nel database — nessuna key viene memorizzata in chiaro.\n\n🛡️ **Best practices:**\n- Non inserire la key nel codice sorgente — usa variabili d'ambiente\n- Non condividere la key su repository pubblici\n- In caso di compromissione, contatta support@tariffhub.ch per la sostituzione immediata\n\n🔐 **Sessioni dashboard**: token JWT temporanei, non persistenti lato server.\n\n📡 **Trasporto**: tutto il traffico su HTTPS.",
            "de": "## Sicherheit\n\n🔑 **Authentifizierung**: Jede API-Anfrage muss den Key im Header enthalten:\n```\nAuthorization: Bearer stk_xxxxxxxxxxxx\n```\n\n🔒 **Hashing**: API-Keys werden mit **SHA-256** gehasht — keine Keys im Klartext gespeichert.\n\n🛡️ **Best Practices**: Key nicht im Quellcode, Umgebungsvariablen verwenden, nicht in öffentlichen Repos.",
            "fr": "## Sécurité\n\n🔑 **Authentification** : chaque requête API doit inclure la clé dans l'en-tête :\n```\nAuthorization: Bearer stk_xxxxxxxxxxxx\n```\n\n🔒 **Hachage** : les clés API sont hachées avec **SHA-256** — aucune clé en clair dans la base de données.\n\n🛡️ **Bonnes pratiques** : ne pas mettre la clé dans le code source, utiliser des variables d'environnement.",
            "en": "## Security\n\n🔑 **Authentication**: every API request must include the key in the header:\n```\nAuthorization: Bearer stk_xxxxxxxxxxxx\n```\n\n🔒 **Hashing**: API keys are hashed with **SHA-256** in the database — no key is stored in plaintext.\n\n🛡️ **Best practices:**\n- Don't hardcode the key in source code — use environment variables\n- Don't share the key in public repositories\n- If compromised, contact support@tariffhub.ch for immediate replacement\n\n🔐 **Dashboard sessions**: temporary JWT tokens, not persisted server-side.\n\n📡 **Transport**: all traffic over HTTPS.",
        },
        "tags": {"security", "auth", "technical"},
        "priority": 1.0,
    },

    # ── PROVIDER CKW ─────────────────────────────────────────────────────────
    {
        "id": "provider_ckw",
        "phrases": [
            "ckw zentralschweiz svizzera centrale tariff api url",
            "ckw central switzerland tariff api url endpoint",
            "ckw ckw_home_dynamic ckw_business_dynamic tariff id",
            "ckw provider informazioni tariffa dinamica",
            "ckw provider information dynamic tariff",
        ],
        "answers": {
            "it": "## Provider CKW\n\n**CKW** (Centralschweizerische Kraftwerke) — Svizzera Centrale\n\n🆔 **Tariff ID**: `ckw_home_dynamic`, `ckw_business_dynamic`\n⏰ **Aggiornamento**: ogni giorno alle **11:05 UTC** (prima di tutti gli altri)\n🔌 **API reale**: `https://e-ckw-public-data.de-c1.eu1.cloudhub.io/api/v1/netzinformationen/energie/dynamische-preise`\n\n✅ CKW è il provider di **riferimento** del sistema — ha la knowledge più lunga e l'aggiornamento più precoce.",
            "de": "## Anbieter CKW\n\n**CKW** (Centralschweizerische Kraftwerke) — Zentralschweiz\n\n🆔 **Tariff-ID**: `ckw_home_dynamic`, `ckw_business_dynamic`\n⏰ **Aktualisierung**: täglich um **11:05 UTC**\n🔌 **Echtzeit-API**: `https://e-ckw-public-data.de-c1.eu1.cloudhub.io/...`\n\n✅ CKW ist der **Referenz-Anbieter** des Systems.",
            "fr": "## Fournisseur CKW\n\n**CKW** (Centralschweizerische Kraftwerke) — Suisse centrale\n\n🆔 **Tariff ID** : `ckw_home_dynamic`, `ckw_business_dynamic`\n⏰ **Mise à jour** : tous les jours à **11:05 UTC**\n\n✅ CKW est le fournisseur de **référence** du système.",
            "en": "## Provider CKW\n\n**CKW** (Centralschweizerische Kraftwerke) — Central Switzerland\n\n🆔 **Tariff ID**: `ckw_home_dynamic`, `ckw_business_dynamic`\n⏰ **Update**: daily at **11:05 UTC** (earliest of all providers)\n🔌 **Real API**: `https://e-ckw-public-data.de-c1.eu1.cloudhub.io/api/v1/netzinformationen/energie/dynamische-preise`\n\n✅ CKW is the system's **reference provider** — longest data history and earliest daily update.",
        },
        "tags": {"provider", "ckw"},
        "priority": 1.0,
    },

    # ── PROVIDER EKZ ─────────────────────────────────────────────────────────
    {
        "id": "provider_ekz",
        "phrases": [
            "ekz zurigo zurich tariff api url energie dynamisch",
            "ekz zurich tariff api url dynamic energy",
            "ekz ekz_energie_dynamisch_netz_400d tariff id",
            "ekz prezzi uniformi annuali fixed annual prices",
            "ekz provider informazioni tariffa dinamica",
        ],
        "answers": {
            "it": "## Provider EKZ\n\n**EKZ** (Elektrizitätswerke des Kantons Zürich) — Zurigo\n\n🆔 **Tariff ID**: `ekz_energie_dynamisch_netz_400d`\n⏰ **Aggiornamento**: ogni giorno alle **17:30 UTC**\n🔌 **API reale**: `https://api.tariffs.ekz.ch`\n\n⚠️ **Nota tecnica**: EKZ pubblica prezzi annuali fissi a dicembre — questo causa falsi alert di anomalia nel sistema, ma i dati sono corretti.",
            "en": "## Provider EKZ\n\n**EKZ** (Elektrizitätswerke des Kantons Zürich) — Zurich\n\n🆔 **Tariff ID**: `ekz_energie_dynamisch_netz_400d`\n⏰ **Update**: daily at **17:30 UTC**\n🔌 **Real API**: `https://api.tariffs.ekz.ch`\n\n⚠️ **Technical note**: EKZ publishes fixed annual prices in December — this causes false anomaly alerts in the system, but data is correct.",
            "de": "## Anbieter EKZ\n\n**EKZ** (Elektrizitätswerke des Kantons Zürich) — Zürich\n\n🆔 **Tariff-ID**: `ekz_energie_dynamisch_netz_400d`\n⏰ **Aktualisierung**: täglich um **17:30 UTC**\n\n⚠️ **Hinweis**: EKZ veröffentlicht im Dezember feste Jahrespreise.",
            "fr": "## Fournisseur EKZ\n\n**EKZ** (Elektrizitätswerke des Kantons Zürich) — Zurich\n\n🆔 **Tariff ID** : `ekz_energie_dynamisch_netz_400d`\n⏰ **Mise à jour** : tous les jours à **17:30 UTC**",
        },
        "tags": {"provider", "ekz"},
        "priority": 1.0,
    },

    # ── PROVIDER GROUPE E ─────────────────────────────────────────────────────
    {
        "id": "provider_groupe_e",
        "phrases": [
            "groupe e groupe-e svizzera occidentale romandia romandie tariffa",
            "groupe e western switzerland romandy tariff",
            "groupe e prezzi negativi energy null residual null",
            "groupe e negative prices energy null residual null",
            "groupe e gridflex integrated tariff api v2",
        ],
        "answers": {
            "it": "## Provider Groupe E\n\n**Groupe E** — Svizzera Occidentale (Romandia)\n\n🆔 **Tariff ID**: `groupe_e_...` (vedi `/api/v1/tariffs`)\n⏰ **Aggiornamento**: ogni giorno alle **17:30 UTC**\n\n⚠️ **Caratteristiche speciali:**\n- Solo i campi `grid_price_utc` e `integrated` sono popolati\n- `energy_price_utc` e `residual_price_utc` sono `null` per design\n- I **prezzi negativi** sono validi (surplus di rete) — non sono errori\n- API upstream usa il percorso `v2` (non `v1`)",
            "en": "## Provider Groupe E\n\n**Groupe E** — Western Switzerland (Romandy)\n\n🆔 **Tariff ID**: `groupe_e_...` (see `/api/v1/tariffs`)\n⏰ **Update**: daily at **17:30 UTC**\n\n⚠️ **Special characteristics:**\n- Only `grid_price_utc` and `integrated` fields are populated\n- `energy_price_utc` and `residual_price_utc` are `null` by design\n- **Negative prices** are valid (grid surplus) — they are not errors\n- Upstream API uses `v2` path (not `v1`)",
            "de": "## Anbieter Groupe E\n\n**Groupe E** — Westschweiz (Romandie)\n\n⚠️ **Besondere Eigenschaften**: Nur `grid_price_utc` und `integrated` sind befüllt. Negative Preise sind gültig (Netzüberschuss).",
            "fr": "## Fournisseur Groupe E\n\n**Groupe E** — Suisse occidentale (Romandie)\n\n⚠️ **Caractéristiques spéciales** : Seuls `grid_price_utc` et `integrated` sont renseignés. Les prix négatifs sont valides (surplus réseau).",
        },
        "tags": {"provider", "groupe_e"},
        "priority": 1.0,
    },

    # ── TUTTI I PROVIDER ──────────────────────────────────────────────────────
    {
        "id": "all_providers",
        "phrases": [
            "tutti i provider fornitori lista elenco provider supportati EVU svizzeri",
            "all providers suppliers list supported providers swiss EVUs",
            "alle anbieter liste unterstützte anbieter schweizer EVU",
            "tous les fournisseurs liste fournisseurs supportés EVU suisses",
            "quanti provider ci sono copertura provider disponibili",
            "how many providers coverage available providers",
            "provider attivi inattivi ail ega perché non attivi",
            "active inactive providers ail ega why not active",
        ],
        "answers": {
            "it": "## Provider supportati (8 attivi)\n\n| Provider | Regione | Tariff ID | Stato |\n|----------|---------|-----------|-------|\n| **CKW** | Svizzera Centrale | `ckw_home_dynamic`, `ckw_business_dynamic` | ✅ Attivo |\n| **EKZ** | Zurigo | `ekz_energie_dynamisch_netz_400d` | ✅ Attivo |\n| **EKZ Einsiedeln** | Einsiedeln (SZ) | `ekz_einsiedeln_...` | ✅ Attivo |\n| **Primeo Energie** | Basilea / Argovia | `primeo_...` | ✅ Attivo |\n| **AVAG** | Aare/Berna | `avag_...` | ✅ Attivo |\n| **ELAG** | Gretzenbach (SO) | `elag_...` | ✅ Attivo |\n| **AEM** | Massagno (TI) | `aem_...` | ✅ Attivo |\n| **Groupe E** | Romandia | `groupe_e_...` | ✅ Attivo |\n| **AIL** | Lugano (TI) | — | ⛔ Nessuna API pubblica |\n| **EGA** | Aettenschwil (AG) | `ega_...` | ⛔ Auth cliente richiesta |\n\n📍 Totale: **457 comuni** coperti su 2.148",
            "de": "## Unterstützte Anbieter (8 aktiv)\n\n| Anbieter | Region | Tariff-ID | Status |\n|----------|--------|-----------|--------|\n| **CKW** | Zentralschweiz | `ckw_home_dynamic`, `ckw_business_dynamic` | ✅ Aktiv |\n| **EKZ** | Zürich | `ekz_...` | ✅ Aktiv |\n| **EKZ Einsiedeln** | Einsiedeln (SZ) | `ekz_einsiedeln_...` | ✅ Aktiv |\n| **Primeo** | Basel/Aargau | `primeo_...` | ✅ Aktiv |\n| **AVAG** | Aare/Bern | `avag_...` | ✅ Aktiv |\n| **ELAG** | Gretzenbach (SO) | `elag_...` | ✅ Aktiv |\n| **AEM** | Massagno (TI) | `aem_...` | ✅ Aktiv |\n| **Groupe E** | Romandie | `groupe_e_...` | ✅ Aktiv |\n| **AIL** | Lugano (TI) | — | ⛔ Keine öffentliche API |\n| **EGA** | Aettenschwil (AG) | `ega_...` | ⛔ Kunden-Auth erforderlich |",
            "fr": "## Fournisseurs supportés (8 actifs)\n\n| Fournisseur | Région | Tariff ID | Statut |\n|-------------|--------|-----------|--------|\n| **CKW** | Suisse centrale | `ckw_...` | ✅ Actif |\n| **EKZ** | Zurich | `ekz_...` | ✅ Actif |\n| **EKZ Einsiedeln** | Einsiedeln | `ekz_einsiedeln_...` | ✅ Actif |\n| **Primeo** | Bâle / Argovie | `primeo_...` | ✅ Actif |\n| **AVAG** | Argovie/Berne | `avag_...` | ✅ Actif |\n| **ELAG** | Gretzenbach | `elag_...` | ✅ Actif |\n| **AEM** | Massagno (TI) | `aem_...` | ✅ Actif |\n| **Groupe E** | Romandie | `groupe_e_...` | ✅ Actif |\n| **AIL** | Lugano | — | ⛔ Pas d'API publique |\n| **EGA** | Aettenschwil | `ega_...` | ⛔ Auth client requise |",
            "en": "## Supported Providers (8 active)\n\n| Provider | Region | Tariff ID | Status |\n|----------|--------|-----------|--------|\n| **CKW** | Central Switzerland | `ckw_home_dynamic`, `ckw_business_dynamic` | ✅ Active |\n| **EKZ** | Zurich | `ekz_energie_dynamisch_netz_400d` | ✅ Active |\n| **EKZ Einsiedeln** | Einsiedeln (SZ) | `ekz_einsiedeln_...` | ✅ Active |\n| **Primeo Energie** | Basel / Aargau | `primeo_...` | ✅ Active |\n| **AVAG** | Aare/Bern | `avag_...` | ✅ Active |\n| **ELAG** | Gretzenbach (SO) | `elag_...` | ✅ Active |\n| **AEM** | Massagno (TI) | `aem_...` | ✅ Active |\n| **Groupe E** | Romandy | `groupe_e_...` | ✅ Active |\n| **AIL** | Lugano (TI) | — | ⛔ No public API |\n| **EGA** | Aettenschwil (AG) | `ega_...` | ⛔ Customer auth required |\n\n📍 Total: **457 municipalities** covered out of 2,148",
        },
        "tags": {"providers", "all", "coverage"},
        "priority": 1.0,
    },

    # ── CONTATTI ──────────────────────────────────────────────────────────────
    {
        "id": "contact",
        "phrases": [
            "contatto email supporto come contattare scrivere assistenza",
            "contact email support how to contact write assistance",
            "kontakt e-mail support wie kontaktieren schreiben hilfe",
            "contact e-mail support comment contacter écrire assistance",
            "support@tariffhub.ch problema tecnico errore",
            "support@tariffhub.ch technical problem error bug",
            "non funziona problema errore 500 400 401",
            "not working problem error 500 400 401",
            "tempo di risposta supporto ore lavorative",
            "response time support business hours",
        ],
        "answers": {
            "it": "## Contatti\n\n📧 **Email**: support@tariffhub.ch\n\n⏰ **Risposta**: entro 24 ore nei giorni lavorativi (lun–ven)\n\n**Per cosa contattarci:**\n- API key smarrita o compromessa\n- Problemi tecnici con l'integrazione\n- Richieste di upgrade/downgrade piano\n- Segnalazione di dati anomali\n- Domande commerciali\n\n💡 Per problemi urgenti indica nell'oggetto: `[URGENTE] Swiss Tariff Hub`",
            "de": "## Kontakt\n\n📧 **E-Mail**: support@tariffhub.ch\n\n⏰ **Antwortzeit**: innerhalb von 24 Stunden an Werktagen (Mo–Fr)\n\n**Wofür uns kontaktieren:** Verlorene API-Keys, technische Probleme, Plan-Upgrades, anomale Daten, kommerzielle Anfragen.",
            "fr": "## Contact\n\n📧 **E-mail** : support@tariffhub.ch\n\n⏰ **Réponse** : dans les 24 heures les jours ouvrables (lun–ven)\n\n**Pour nous contacter** : clé API perdue, problèmes techniques, changement de plan, données anomales, questions commerciales.",
            "en": "## Contact\n\n📧 **Email**: support@tariffhub.ch\n\n⏰ **Response**: within 24 hours on business days (Mon–Fri)\n\n**Reasons to contact us:**\n- Lost or compromised API key\n- Technical integration problems\n- Plan upgrade/downgrade requests\n- Anomalous data reports\n- Commercial inquiries\n\n💡 For urgent issues add to subject: `[URGENT] Swiss Tariff Hub`",
        },
        "tags": {"contact", "support"},
        "priority": 1.0,
    },

    # ── HEALTH / STATO SISTEMA ────────────────────────────────────────────────
    {
        "id": "health",
        "phrases": [
            "stato sistema health status uptime monitoring api down",
            "system health status uptime monitoring api down",
            "systemstatus uptime überwachung api ausgefallen",
            "état du système uptime surveillance api hors service",
            "api non risponde down errore sistema",
            "api not responding down system error",
            "ultimo aggiornamento fetch data ultimo log",
            "last update fetch data last log",
        ],
        "answers": {
            "it": "## Stato del sistema\n\n📊 **Endpoint health**: `/api/v1/health`\nRitorna: uptime, versione, status per ogni provider, ora ultimo fetch.\n\n✅ **Indicatore header**: il dot verde in cima alla dashboard mostra quante tariffe hanno dati di oggi.\n\n🔔 **Monitoring**: il sistema usa Healthchecks.io + UptimeRobot per notifiche automatiche in caso di downtime.\n\n🔁 **Circuit breaker**: dopo 3 fetch consecutivi falliti, un provider viene sospeso fino al giorno successivo.\n\nSe vedi un provider con badge \"pending\" in cima: i dati di oggi non sono ancora stati recuperati (normale prima delle 17:30 UTC).",
            "en": "## System Health\n\n📊 **Health endpoint**: `/api/v1/health`\nReturns: uptime, version, per-provider status, last fetch time.\n\n✅ **Header indicator**: the green dot at the top of the dashboard shows how many tariffs have today's data.\n\n🔔 **Monitoring**: the system uses Healthchecks.io + UptimeRobot for automatic downtime notifications.\n\n🔁 **Circuit breaker**: after 3 consecutive failed fetches, a provider is suspended until the next day.\n\nIf you see a provider with a \"pending\" badge: today's data hasn't been fetched yet (normal before 17:30 UTC).",
            "de": "## Systemstatus\n\n📊 **Health-Endpunkt**: `/api/v1/health`\nGibt zurück: Uptime, Version, Anbieter-Status, letzter Abruf.\n\n✅ Grüner Punkt im Header zeigt Tarife mit heutigen Daten.\n\n🔁 **Circuit Breaker**: nach 3 Fehlversuchen wird Anbieter bis zum nächsten Tag gesperrt.",
            "fr": "## État du système\n\n📊 **Endpoint health** : `/api/v1/health`\nRetourne : uptime, version, statut par fournisseur, dernière récupération.\n\n✅ Le point vert en haut du dashboard indique les tarifs avec données du jour.",
        },
        "tags": {"health", "monitoring", "technical"},
        "priority": 1.0,
    },

    # ── TARIFF ID ─────────────────────────────────────────────────────────────
    {
        "id": "tariff_id",
        "phrases": [
            "tariff id come si usa dove lo trovo lista tariff id",
            "tariff id how to use where to find list of tariff ids",
            "tariff id wie verwenden wo finden liste tariff ids",
            "tariff id comment utiliser où trouver liste des tariff ids",
            "quale tariff id usare per provider x",
            "which tariff id to use for provider x",
            "ckw_home_dynamic ekz_energie_dynamisch_netz_400d format tariff id",
        ],
        "answers": {
            "it": "## Tariff ID\n\nOgni tariffa ha un **ID univoco** nel formato `provider_descrizione`.\n\n**Come ottenerli**: chiama `GET /api/v1/tariffs` — ritorna la lista completa con ID, nome, provider e data disponibilità.\n\n**Esempi:**\n- `ckw_home_dynamic` → CKW residenziale\n- `ckw_business_dynamic` → CKW business\n- `ekz_energie_dynamisch_netz_400d` → EKZ Zurigo\n- `groupe_e_...` → Groupe E (Romandia)\n\n**Uso negli endpoint:**\n```\nGET /api/v1/prices/today?tariff_id=ckw_home_dynamic\n```",
            "en": "## Tariff ID\n\nEach tariff has a **unique ID** in the format `provider_description`.\n\n**How to get them**: call `GET /api/v1/tariffs` — returns the full list with ID, name, provider and availability date.\n\n**Examples:**\n- `ckw_home_dynamic` → CKW residential\n- `ckw_business_dynamic` → CKW business\n- `ekz_energie_dynamisch_netz_400d` → EKZ Zurich\n\n**Usage in endpoints:**\n```\nGET /api/v1/prices/today?tariff_id=ckw_home_dynamic\n```",
            "de": "## Tariff-ID\n\nJeder Tarif hat eine **eindeutige ID** im Format `anbieter_beschreibung`.\n\n**So abrufen**: `GET /api/v1/tariffs` aufrufen — gibt die vollständige Liste zurück.",
            "fr": "## Tariff ID\n\nChaque tarif a un **ID unique** au format `fournisseur_description`.\n\n**Comment les obtenir** : appeler `GET /api/v1/tariffs` — retourne la liste complète.",
        },
        "tags": {"tariff_id", "api", "technical"},
        "priority": 1.0,
    },

    # ── PREZZI DOMANI ─────────────────────────────────────────────────────────
    {
        "id": "tomorrow_prices",
        "phrases": [
            "prezzi domani quando disponibili day ahead domani",
            "tomorrow prices when available day ahead",
            "preise morgen wann verfügbar day ahead",
            "prix demain quand disponibles day ahead",
            "prices not yet available tomorrow why",
            "prezzi domani non disponibili perché",
            "dopo che ora sono disponibili i prezzi di domani",
            "after what time are tomorrow's prices available",
        ],
        "answers": {
            "it": "## Prezzi di domani\n\nI prezzi del giorno successivo (day-ahead) vengono pubblicati dai provider svizzeri solitamente nel pomeriggio.\n\n⏰ **Disponibilità**: tipicamente dopo le **17:30 UTC** (19:30 ora svizzera)\n\n**Endpoint**: `GET /api/v1/prices/tomorrow?tariff_id=X`\n\n⚠️ **Prima delle 17:30 UTC**: l'endpoint risponde con `404` o dati vuoti — è normale.\n\n🔁 Il sistema fetcha automaticamente alle 17:30 UTC per tutti i provider (CKW alle 11:05 UTC).",
            "en": "## Tomorrow's Prices\n\nNext-day (day-ahead) prices are published by Swiss providers typically in the afternoon.\n\n⏰ **Availability**: typically after **17:30 UTC** (19:30 Swiss time)\n\n**Endpoint**: `GET /api/v1/prices/tomorrow?tariff_id=X`\n\n⚠️ **Before 17:30 UTC**: the endpoint returns `404` or empty data — this is normal.\n\n🔁 The system fetches automatically at 17:30 UTC for all providers (CKW at 11:05 UTC).",
            "de": "## Preise für morgen\n\n⏰ **Verfügbarkeit**: typischerweise nach **17:30 UTC** (19:30 Schweizer Zeit)\n\n**Endpunkt**: `GET /api/v1/prices/tomorrow?tariff_id=X`\n\n⚠️ Vor 17:30 UTC gibt der Endpunkt `404` zurück — das ist normal.",
            "fr": "## Prix de demain\n\n⏰ **Disponibilité** : typiquement après **17:30 UTC** (19:30 heure suisse)\n\n**Endpoint** : `GET /api/v1/prices/tomorrow?tariff_id=X`\n\n⚠️ Avant 17:30 UTC, l'endpoint retourne `404` — c'est normal.",
        },
        "tags": {"tomorrow", "day_ahead", "prices", "schedule"},
        "priority": 1.0,
    },

    # ── LIMITE DI RICHIESTE / RATE LIMIT ─────────────────────────────────────
    {
        "id": "rate_limit",
        "phrases": [
            "limite richieste al giorno rate limit 429 troppo poche richieste",
            "request limit per day rate limit 429 too many requests",
            "anfragenlimit pro tag ratenlimit 429 zu viele anfragen",
            "limite requêtes par jour rate limit 429 trop de requêtes",
            "ho esaurito le richieste giornaliere quando si azzera",
            "i used up daily requests when does it reset",
            "500 richieste gratuite 2000 pro requests daily",
            "free 500 requests pro 2000 daily limit",
        ],
        "answers": {
            "it": "## Rate limit\n\n| Piano | Limite giornaliero | Reset |\n|-------|-------------------|-------|\n| **Free** | 500 richieste/giorno | Mezzanotte UTC |\n| **Pro** | 2.000 richieste/giorno | Mezzanotte UTC |\n\n⚠️ Superato il limite, le richieste ricevono risposta `429 Too Many Requests`.\n\n🔄 **Reset**: ogni giorno a **mezzanotte UTC** il contatore viene azzerato.\n\n📊 **Monitoraggio**: puoi verificare il tuo uso attuale nel pannello utente della dashboard (tab API → tuo profilo → barra di utilizzo).\n\n💡 Se 2.000 richieste/giorno non bastano, contatta support@tariffhub.ch per un piano enterprise.",
            "en": "## Rate Limit\n\n| Plan | Daily limit | Reset |\n|------|-------------|-------|\n| **Free** | 500 requests/day | Midnight UTC |\n| **Pro** | 2,000 requests/day | Midnight UTC |\n\n⚠️ After the limit is reached, requests get `429 Too Many Requests`.\n\n🔄 **Reset**: every day at **midnight UTC** the counter resets.\n\n📊 **Monitoring**: check your current usage in the user panel (API tab → your profile → usage bar).\n\n💡 If 2,000 requests/day isn't enough, contact support@tariffhub.ch for an enterprise plan.",
            "de": "## Ratenlimit\n\n| Plan | Tageslimit | Reset |\n|------|-----------|-------|\n| **Free** | 500 Anfragen/Tag | Mitternacht UTC |\n| **Pro** | 2.000 Anfragen/Tag | Mitternacht UTC |\n\n⚠️ Nach Erreichen des Limits: `429 Too Many Requests`.\n🔄 Reset täglich um **Mitternacht UTC**.",
            "fr": "## Limite de requêtes\n\n| Plan | Limite quotidienne | Reset |\n|------|--------------------|-------|\n| **Free** | 500 requêtes/jour | Minuit UTC |\n| **Pro** | 2 000 requêtes/jour | Minuit UTC |\n\n⚠️ Après dépassement : `429 Too Many Requests`.\n🔄 Reset quotidien à **minuit UTC**.",
        },
        "tags": {"rate_limit", "quota", "api", "plans"},
        "priority": 1.0,
    },

    # ── BACKFILL / STORICO ────────────────────────────────────────────────────
    {
        "id": "history",
        "phrases": [
            "storico dati storici backfill dati passati giorni passati",
            "historical data backfill past data past days history",
            "historische daten backfill vergangene tage verlauf",
            "données historiques backfill jours passés historique",
            "quanto storico è disponibile da quando ci sono i dati",
            "how much history is available since when is data available",
            "dati del passato mesi precedenti anno scorso",
            "past data previous months last year",
            "datetime_available_from_utc da quando disponibile",
        ],
        "answers": {
            "it": "## Storico dei dati\n\nLo storico disponibile varia per provider:\n- **CKW**: storico dalla data di onboarding nel sistema\n- **Altri provider**: dipende dall'API upstream — non tutti supportano il backfill\n\n📅 **Data disponibilità**: ogni tariffa ha il campo `datetime_available_from_utc` in `/api/v1/tariffs`.\n\n⚠️ **Backfill**: alcuni provider (Primeo, AEM, EKZ) ignorano il parametro `?date=` e restituiscono sempre il giorno corrente — il backfill non è disponibile per questi.\n\n🔒 **Accesso storico:**\n- Guest: solo oggi\n- Free: storico della tariffa scelta\n- Pro: storico illimitato di tutte le tariffe",
            "en": "## Data History\n\nAvailable history varies by provider:\n- **CKW**: history from system onboarding date\n- **Other providers**: depends on upstream API — not all support backfill\n\n📅 **Availability date**: each tariff has `datetime_available_from_utc` in `/api/v1/tariffs`.\n\n⚠️ **Backfill**: some providers (Primeo, AEM, EKZ) ignore the `?date=` parameter and always return the current day — backfill not available for these.\n\n🔒 **History access:**\n- Guest: today only\n- Free: history for chosen tariff\n- Pro: unlimited history for all tariffs",
            "de": "## Datenverlauf\n\nVerfügbare Historie variiert je Anbieter. Einige (Primeo, AEM, EKZ) unterstützen kein Backfill.\n\n🔒 **Zugang**: Gast nur heute · Free gewählter Tarif · Pro alles.",
            "fr": "## Historique des données\n\nL'historique disponible varie selon le fournisseur. Certains (Primeo, AEM, EKZ) ne supportent pas le backfill.\n\n🔒 **Accès** : invité aujourd'hui seulement · Free tarif choisi · Pro illimité.",
        },
        "tags": {"history", "backfill", "data", "plans"},
        "priority": 1.0,
    },
]

# ─────────────────────────────────────────────────────────────────────────────
#  FALLBACK MULTILINGUE
# ─────────────────────────────────────────────────────────────────────────────
_FALLBACK = {
    "it": "Non ho trovato una risposta precisa a questa domanda. Prova a riformularla oppure scrivici a **support@tariffhub.ch** — rispondiamo entro 24 ore! 📧\n\n💡 Puoi chiedermi di: piani, registrazione, login, provider, endpoint API, formato dati, mappa, orari aggiornamento.",
    "de": "Ich konnte keine genaue Antwort auf diese Frage finden. Versuchen Sie, sie umzuformulieren, oder schreiben Sie uns an **support@tariffhub.ch**! 📧\n\n💡 Mögliche Themen: Pläne, Registrierung, Login, Anbieter, API-Endpunkte, Datenformat, Karte, Aktualisierungszeiten.",
    "fr": "Je n'ai pas trouvé de réponse précise. Essayez de reformuler ou écrivez-nous à **support@tariffhub.ch** ! 📧\n\n💡 Sujets possibles : plans, inscription, connexion, fournisseurs, endpoints API, format des données, carte, horaires.",
    "en": "I couldn't find a precise answer to this question. Try rephrasing it, or contact us at **support@tariffhub.ch** — we reply within 24 hours! 📧\n\n💡 You can ask me about: plans, registration, login, providers, API endpoints, data format, map, update schedules.",
}

# ─────────────────────────────────────────────────────────────────────────────
#  LANGUAGE DETECTION
# ─────────────────────────────────────────────────────────────────────────────
_LANG_SIGNALS = {
    "it": ["cosa","come","quando","perché","quale","quali","puoi","posso","ho",
           "hai","non","anche","però","quindi","allora","ciao","grazie","aiuto",
           "vorrei","dimmi","spiegami","funziona","funzionano","dove","chi"],
    "de": ["was","wie","wenn","warum","welche","welcher","können","bitte",
           "danke","hallo","zeig","kostet","ich","nicht","kein","mehr","noch",
           "gibt","haben","sein","mein","dein","möchte","wann","wo"],
    "fr": ["qu'est","comment","pouvez","merci","bonjour","montrez","quels","quel",
           "coûte","pourquoi","une","des","les","dans","sur","avec","est","sont",
           "pour","par","que","qui","mais","aussi","même"],
}

def _detect_lang(text: str, hint: Optional[str]) -> str:
    if hint and hint in ("it", "de", "fr", "en"):
        return hint
    t = text.lower()
    scores = {lang: sum(1 for s in signals if s in t)
              for lang, signals in _LANG_SIGNALS.items()}
    best_lang = max(scores, key=scores.get)
    return best_lang if scores[best_lang] > 0 else "en"


# ─────────────────────────────────────────────────────────────────────────────
#  INDEX BUILD (lazy, singleton)
# ─────────────────────────────────────────────────────────────────────────────
_corpus: Optional[TFIDFCorpus] = None
_intent_map: dict[str, dict] = {}

def _ensure_index():
    global _corpus, _intent_map
    if _corpus is not None:
        return
    _corpus = TFIDFCorpus()
    for intent in _KB:
        _intent_map[intent["id"]] = intent
        # Combina tutte le frasi trigger in un unico documento per l'intent
        all_text = " ".join(intent["phrases"])
        tokens = _tokenize(all_text)
        _corpus.add(intent["id"], tokens)


# ─────────────────────────────────────────────────────────────────────────────
#  CONTEXT WINDOW — analisi conversazione precedente
# ─────────────────────────────────────────────────────────────────────────────
def _extract_context_boost(messages: list[dict]) -> dict[str, float]:
    """
    Analizza i turni recenti e booста gli intent tematicamente correlati.
    Restituisce {intent_id: boost_multiplier}.
    """
    boosts: dict[str, float] = {}
    # Leggi gli ultimi 4 messaggi (2 turni)
    recent = [m["content"] for m in messages[-4:] if m.get("role") == "user"]
    if not recent:
        return boosts
    combined = " ".join(recent).lower()

    # Tag → intent_ids mapping
    tag_intent_map: dict[str, list[str]] = defaultdict(list)
    for intent in _KB:
        for tag in intent.get("tags", set()):
            tag_intent_map[tag].append(intent["id"])

    # Se il contesto ha token che suggeriscono un dominio, boost quel dominio
    context_tokens = set(_tokenize(combined))
    for intent in _KB:
        intent_tokens = set(_tokenize(" ".join(intent["phrases"])))
        overlap = len(context_tokens & intent_tokens)
        if overlap >= 2:
            boosts[intent["id"]] = 1.0 + (overlap * 0.1)

    return boosts


# ─────────────────────────────────────────────────────────────────────────────
#  MAIN ENGINE
# ─────────────────────────────────────────────────────────────────────────────
# Soglie
_THRESHOLD_HIGH   = 0.25   # risposta diretta
_THRESHOLD_MEDIUM = 0.10   # risposta + disclaimer
_THRESHOLD_LOW    = 0.04   # fallback

def smart_reply(messages: list[dict], hint_lang: Optional[str] = None) -> str:
    """
    Entry point principale del motore.
    
    Args:
        messages: lista di dict {"role": "user"|"assistant", "content": str}
        hint_lang: lingua suggerita dal frontend ("it","de","fr","en" o None)
    
    Returns:
        Stringa risposta in markdown.
    """
    _ensure_index()

    user_msgs = [m for m in messages if m.get("role") == "user"]
    if not user_msgs:
        lang = hint_lang or "it"
        intent = _intent_map.get("greeting")
        return intent["answers"].get(lang, intent["answers"]["en"]) if intent else _FALLBACK.get(lang, _FALLBACK["en"])

    last_msg = user_msgs[-1]["content"].strip()
    if not last_msg:
        return _FALLBACK.get(hint_lang or "en", _FALLBACK["en"])

    lang = _detect_lang(last_msg, hint_lang)
    query_tokens = _tokenize(last_msg)

    # Query TF-IDF
    results = _corpus.query(query_tokens, top_k=5)

    # Context boost
    context_boosts = _extract_context_boost(messages[:-1])  # escludi ultimo messaggio

    # Applica boost priorità e contesto
    scored: list[tuple[str, float]] = []
    for intent_id, score in results:
        intent = _intent_map[intent_id]
        priority = intent.get("priority", 1.0)
        ctx_boost = context_boosts.get(intent_id, 1.0)
        final_score = score * priority * ctx_boost
        scored.append((intent_id, final_score))

    scored.sort(key=lambda x: x[1], reverse=True)

    # Selezione risposta
    if not scored or scored[0][1] < _THRESHOLD_LOW:
        return _FALLBACK.get(lang, _FALLBACK["en"])

    best_id, best_score = scored[0]
    best_intent = _intent_map[best_id]

    answer = best_intent["answers"].get(lang) or best_intent["answers"].get("en", "")

    # Se score medio, aggiungi nota "non sono sicuro"
    if best_score < _THRESHOLD_MEDIUM:
        prefix = {
            "it": "💬 *Potrei non aver capito perfettamente la domanda, ma ecco quello che so:*\n\n",
            "de": "💬 *Ich bin mir nicht ganz sicher, aber hier ist was ich weiß:*\n\n",
            "fr": "💬 *Je ne suis pas sûr d'avoir bien compris, mais voici ce que je sais :*\n\n",
            "en": "💬 *I may not have understood perfectly, but here's what I know:*\n\n",
        }.get(lang, "")
        answer = prefix + answer

    # Se ci sono più intent rilevanti con score vicino, aggiungi suggerimenti correlati
    if len(scored) >= 2 and scored[1][1] > _THRESHOLD_LOW * 2:
        related_ids = [sid for sid, _ in scored[1:3] if sid != best_id]
        if related_ids:
            related_labels = {
                "it": "📌 *Argomenti correlati che potresti voler esplorare:* ",
                "de": "📌 *Verwandte Themen, die Sie erkunden möchten:* ",
                "fr": "📌 *Sujets connexes que vous pourriez explorer :* ",
                "en": "📌 *Related topics you might want to explore:* ",
            }.get(lang, "📌 *Related:* ")
            labels = []
            for rid in related_ids:
                ri = _intent_map.get(rid)
                if ri:
                    tags = ", ".join(list(ri.get("tags", set()))[:2])
                    labels.append(f"`{tags}`")
            if labels:
                answer += "\n\n" + related_labels + " · ".join(labels)

    return answer