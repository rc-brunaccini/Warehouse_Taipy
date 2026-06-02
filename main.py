import sqlite3
from datetime import date
from taipy.gui import Gui, State, notify, download
import pandas as pd
import os
import sys
import tempfile
from datetime import datetime
import shutil

BASE_PROJECT_PATH = os.path.dirname(os.path.abspath(__file__))

# Nuove variabili di stato per tabelle e grafici
tbl_fornitori = pd.DataFrame()
tbl_destinazioni = pd.DataFrame()
tbl_prodotti = pd.DataFrame()
tbl_movimenti = pd.DataFrame()
tbl_ultimi_movimenti = pd.DataFrame()

chart_valore_dest = pd.DataFrame()
chart_valore_cat = pd.DataFrame()
chart_trend = pd.DataFrame()
chart_fornitori = pd.DataFrame()

search_query = ""
tbl_risultati = pd.DataFrame()
rebuild_tabella = True
tipo_ricerca = "Prodotti" # Toggle per decidere cosa cercare
filtro_rapido = "Tutti"
filtri_lov = ["Tutti", "Senza Fornitore", "Sottoscorta", "Anomalie Quantità"]

categorie_list = ["Pulizia", "Vestiti", "Cantiere", "Strumenti"]
    
# KPI & Chart State Variables
kpi_valore_uscite = 0.0
kpi_valore_uscite_display = 0,00 
kpi_total_movimenti = 0
kpi_understock = 0
kpi_top_supplier = "N/D"

chart_stock_top10 = pd.DataFrame()
chart_flow_trend = pd.DataFrame()
chart_supplier_dist = pd.DataFrame()

# ─────────────────────────────────────────────────────────────
# 1. CONFIGURAZIONE & DATABASE (sqlite3)
# ─────────────────────────────────────────────────────────────
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.path.join(BASE_DIR, "magazzino.db")
print(f"IL DATABASE SI TROVA QUI: {DB_PATH}") # Controlla questo output!

def get_conn() -> sqlite3.Connection:
    # Verifichiamo se il file esiste davvero prima di connetterci
    if not os.path.exists(DB_PATH):
        print(f"ATTENZIONE: Database non trovato in {DB_PATH}. Verrà creato.")
    
    conn = sqlite3.connect(DB_PATH, check_same_thread=False, timeout=10) # Importante per Taipy e metti timeout
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn

def init_db():
    print(f"Inizializzazione database in: {DB_PATH}")
    with get_conn() as conn:
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS fornitori (
                nome TEXT PRIMARY KEY
            );
            
            CREATE TABLE IF NOT EXISTS destinazioni (
                nome TEXT PRIMARY KEY
            );
            
            CREATE TABLE IF NOT EXISTS prodotti (
                sku TEXT PRIMARY KEY, 
                nome TEXT NOT NULL, 
                fornitore TEXT, 
                sottoscorta INTEGER DEFAULT 0,
                categoria TEXT,
                prezzo_acquisto REAL DEFAULT 0, -- Aggiunta questa riga
                FOREIGN KEY(fornitore) REFERENCES fornitori(nome) ON UPDATE CASCADE
            );
            
            CREATE TABLE IF NOT EXISTS movimenti (
                id INTEGER PRIMARY KEY AUTOINCREMENT, 
                sku TEXT NOT NULL, 
                tipo TEXT CHECK(tipo IN ('IN','OUT')) NOT NULL, 
                quantita INTEGER NOT NULL CHECK(quantita >= 0), 
                data TEXT NOT NULL,
                destinazione TEXT,
                FOREIGN KEY(sku) REFERENCES prodotti(sku) ON UPDATE CASCADE,
                FOREIGN KEY(destinazione) REFERENCES destinazioni(nome) ON UPDATE CASCADE
            );
        """)
        conn.commit()

def aggiorna_tabelle(state: State):
    # Aggiorna i DataFrame per le tabelle
    state.tbl_fornitori = fetch_table_fornitori()
    state.tbl_destinazioni = fetch_table_destinazioni()
    state.tbl_prodotti = fetch_table_prodotti()
    state.tbl_movimenti = fetch_table_movimenti()
    state.tbl_ultimi_movimenti = fetch_ultimi_5_movimenti()
    
    # Aggiorna le liste per i dropdown (LOV)
    state.fornitori_lov = fetch_fornitori_lov()
    state.prodotto_fornitore_lov = state.fornitori_lov
    state.prodotti_lov = fetch_prodotti_lov()
    state.destinazioni_lov = fetch_destinazioni_lov()
    
    notify(state, "info", "📋 Liste e Tabelle aggiornate")

def aggiorna_dashboard(state: State):
    state.ora_attuale = datetime.now().strftime('%d %b %Y - %H:%M')
    # KPI
    valore_numerico = fetch_kpi_valore_uscite_mese()
    state.kpi_valore_uscite = valore_numerico
    
    # 2. Crea la stringa formattata per la UI
    # Usiamo il formato italiano (virgola per decimali)
    state.kpi_valore_uscite_display = f"€ {valore_numerico:,.2f}".replace(",", "X").replace(".", ",").replace("X", ".")
    state.kpi_total_movimenti = fetch_kpi_total_movimenti()
    state.kpi_understock = fetch_kpi_understock()
    state.kpi_top_supplier = fetch_kpi_top_supplier()
    
    # Grafici
    state.chart_valore_dest = fetch_bi_valore_destinazioni()
    state.chart_valore_cat = fetch_bi_valore_categorie()
    state.chart_flow_trend = fetch_bi_flow_trend()
    state.chart_supplier_dist = fetch_bi_supplier_dist()
    
    notify(state, "info", f"Dati aggiornati alle {state.ora_attuale}")# Handler EDIT/DELETE per Taipy Tables

def cerca_dati(state: State):
    s_query = state.search_query.strip()
    query_param = f"%{s_query}%"
    
    with get_conn() as conn:
        # --- LOGICA PRODOTTI CON GIACENZA ---
        if state.tipo_ricerca == "Prodotti":
            # Questa query calcola la giacenza per ogni prodotto filtrato
            sql = """
                SELECT 
                    p.sku, 
                    p.nome, 
                    p.categoria, 
                    p.fornitore, 
                    p.sottoscorta,
                    p.prezzo_acquisto
                    COALESCE(SUM(CASE WHEN m.tipo='IN' THEN m.quantita ELSE -m.quantita END), 0) as giacenza
                FROM prodotti p
                LEFT JOIN movimenti m ON p.sku = m.sku
                WHERE (p.sku LIKE ? OR p.nome LIKE ?)
            """
            
            # Aggiungiamo i filtri rapidi se presenti
            if state.filtro_rapido == "Senza Fornitore":
                sql += " AND (p.fornitore IS NULL OR p.fornitore = '')"
            
            sql += " GROUP BY p.sku"
            
            # Filtro per sottoscorta (va nel HAVING perché la giacenza è un aggregato)
            if state.filtro_rapido == "Sottoscorta":
                sql += " HAVING giacenza <= p.sottoscorta"
                
            params = (query_param, query_param)

        # --- LOGICA MOVIMENTI ---
        elif state.tipo_ricerca == "Movimenti":
            # Usiamo una base fissa e aggiungiamo i pezzi
            sql = """
                SELECT m.id, m.data, m.sku, p.nome as prodotto, m.tipo, m.quantita, m.destinazione
                FROM movimenti m
                LEFT JOIN prodotti p ON m.sku = p.sku
                WHERE (m.sku LIKE ? OR p.nome LIKE ?)
            """
            params = [query_param, query_param] # Usiamo una lista per poter aggiungere pezzi
            
            if state.filtro_rapido == "Anomalie Quantità":
                sql += " AND m.quantita <= 0"
            elif state.filtro_rapido == "Senza Fornitore": 
                sql += " AND m.tipo = 'OUT' AND (m.destinazione IS NULL OR m.destinazione = '')"
                
            sql += " ORDER BY m.data DESC LIMIT 100"
            params = tuple(params) # Convertiamo in tupla per SQLite

        # --- LOGICA DESTINAZIONI ---
        elif state.tipo_ricerca == "Destinazioni":
            sql = "SELECT nome FROM destinazioni WHERE nome LIKE ?"
            params = (query_param,) # NOTA: Qui c'è solo UN parametro!

        # Esecuzione della query con i parametri corretti
        state.tbl_risultati = pd.read_sql_query(sql, conn, params=params)

    # REFRESH UI
    state.rebuild_tabella = False
    state.tbl_risultati = state.tbl_risultati
    state.rebuild_tabella = True

    if state.tbl_risultati.empty:
        notify(state, "info", f"Nessun risultato in {state.tipo_ricerca}")

def fetch_ultimi_5_movimenti() -> pd.DataFrame:
    query = """
        SELECT id, data, sku, tipo, quantita, destinazione 
        FROM movimenti 
        ORDER BY id DESC LIMIT 5
    """
    with get_conn() as conn:
        return pd.read_sql_query(query, conn)
    
def reset_ricerca(state: State):
    state.search_query = ""
    state.filtro_rapido = "Tutti"
    # Accediamo alla variabile tramite 'state'
    state.rebuild_tabella = False 
    state.tbl_risultati = pd.DataFrame()
    cerca_dati(state)
    state.rebuild_tabella = True
    notify(state, "info", "Ricerca resettata")

# Helper per recuperare le Liste di Valori (LOV)
def fetch_fornitori_lov() -> list:
    with get_conn() as conn:
        # Recuperiamo solo i nomi
        return [r['nome'] for r in conn.execute("SELECT nome FROM fornitori ORDER BY nome").fetchall()]

def fetch_prodotti_lov() -> list[tuple]:
    with get_conn() as conn:
        return [(r['sku'], f"{r['nome']} - {r['sku']}") for r in conn.execute("SELECT sku, nome FROM prodotti ORDER BY nome").fetchall()]

# ─────────────────────────────────────────────────────────────
# 2. STATO GLOBALE TAIPY (Variabili iniziali)
# ─────────────────────────────────────────────────────────────
# LOV (List of Values) per i selettori
fornitori_lov: list[tuple] = []
prodotto_fornitore_lov: list[tuple] = []
prodotti_lov: list[tuple] = []
ones_list = []

# Campi dei form
f_nome = ""
d_nome = ""
p_sku = ""
p_nome = ""
p_categoria = categorie_list[0]
p_fornitore_id = None
p_sottoscorta = 0
p_prezzo = 0.0
m_sku = None
m_tipo = "IN"
m_qta = 1
m_data = date.today().isoformat()
m_destinazione = None
ora_attuale = datetime.now().strftime('%d %b %Y - %H:%M')

# ─────────────────────────────────────────────────────────────
# FUNZIONI DI LETTURA PER TABELLE & BI
# ─────────────────────────────────────────────────────────────
def fetch_table_fornitori() -> pd.DataFrame:
    with get_conn() as conn:
        # RIMOSSO 'id,'
        return pd.read_sql_query("SELECT nome FROM fornitori ORDER BY nome", conn)
    
def fetch_table_prodotti() -> pd.DataFrame:
    # RIMOSSO f.id e p.fornitore_id
    query = """
        SELECT sku, nome, fornitore, sottoscorta, categoria 
        FROM prodotti
    """
    with get_conn() as conn:
        return pd.read_sql_query(query, conn)
    
def fetch_table_destinazioni() -> pd.DataFrame:
    # Recuperiamo solo la colonna 'nome' come richiesto
    query = "SELECT nome FROM destinazioni ORDER BY nome ASC"
    try:
        with get_conn() as conn:
            df = pd.read_sql_query(query, conn)
        return df
    except Exception as e:
        print(f"Errore nel recupero destinazioni: {e}")
        return pd.DataFrame(columns=["nome"])

def fetch_table_movimenti() -> pd.DataFrame:
    query = "SELECT id, sku, tipo, quantita, data, destinazione FROM movimenti ORDER BY data DESC"
    with get_conn() as conn:
        return pd.read_sql_query(query, conn)
    

def fetch_bi_trend() -> pd.DataFrame:
    return pd.read_sql_query("""
        SELECT data, SUM(CASE WHEN tipo='IN' THEN quantita ELSE -quantita END) as flusso_netto
        FROM movimenti GROUP BY data ORDER BY data
    """, get_conn())

def fetch_bi_fornitori() -> pd.DataFrame:
    # RIMOSSO f.id
    return pd.read_sql_query("""
        SELECT nome, COUNT(sku) as num_prodotti
        FROM prodotti GROUP BY nome
    """, get_conn())

def fetch_kpi_valore_uscite_mese() -> float:
    try:
        with get_conn() as conn:
            # Scarichiamo tutti i movimenti in uscita con i relativi prezzi
            query = """
                SELECT m.data, m.quantita, p.prezzo_acquisto
                FROM movimenti m
                JOIN prodotti p ON m.sku = p.sku
                WHERE m.tipo = 'OUT'
            """
            df = pd.read_sql_query(query, conn)
            
            if df.empty:
                return 0.0
            
            # Convertiamo la colonna data in formato datetime di Pandas (molto potente)
            df['data'] = pd.to_datetime(df['data'], errors='coerce')
            
            # Otteniamo mese e anno correnti
            oggi = datetime.now()
            
            # Filtriamo: solo i movimenti di questo mese e di questo anno
            mask = (df['data'].dt.month == oggi.month) & (df['data'].dt.year == oggi.year)
            df_mese = df[mask].copy()
            
            if df_mese.empty:
                return 0.0
            
            # Calcoliamo il totale: (quantità * prezzo)
            totale = (df_mese['quantita'] * df_mese['prezzo_acquisto']).sum()
            return float(totale)
            
    except Exception as e:
        print(f"ERRORE CRITICO KPI: {e}")
        return 0.0
    
def fetch_kpi_total_movimenti() -> int:
    # Conteggio totale uscite (non solo mese, o come preferisci)
    # Se vuoi le uscite TOTALI di sempre:
    query = "SELECT COUNT(*) FROM movimenti WHERE tipo = 'OUT'"
    
    # Se invece vuoi le uscite TOTALI del mese:
    # query = "SELECT COUNT(*) FROM movimenti WHERE tipo = 'OUT' AND strftime('%Y-%m', data) = strftime('%Y-%m', 'now')"
    
    try:
        with get_conn() as conn:
            res = conn.execute(query).fetchone()
            return int(res[0]) if res and res[0] is not None else 0
    except Exception as e:
        print(f"Errore KPI Conteggio: {e}")
        return 0

def fetch_kpi_understock() -> int:
    """
    Conta quanti prodotti hanno una giacenza inferiore alla soglia sottoscorta.
    Logica: (Somma IN - Somma OUT) < sottoscorta
    """
    query = """
        SELECT COUNT(*) FROM (
            SELECT p.sku, p.sottoscorta, 
                   COALESCE(SUM(CASE WHEN m.tipo='IN' THEN m.quantita ELSE -m.quantita END), 0) as stock
            FROM prodotti p 
            LEFT JOIN movimenti m ON p.sku = m.sku 
            GROUP BY p.sku
        ) WHERE stock < sottoscorta
    """
    with get_conn() as conn:
        res = conn.execute(query).fetchone()
        return res[0] if res else 0

def fetch_kpi_top_supplier() -> str:
    # SEMPLIFICATA: ora il fornitore è direttamente nel prodotto
    query = "SELECT fornitore FROM prodotti GROUP BY fornitore ORDER BY COUNT(sku) DESC LIMIT 1"
    with get_conn() as conn:
        res = conn.execute(query).fetchone()
        return res[0] if res else "Nessun dato"
def fetch_bi_supplier_dist() -> pd.DataFrame:
    # RIMOSSO f.id
    query = """
        SELECT fornitore as nome, COUNT(sku) as num_prodotti
        FROM prodotti 
        GROUP BY fornitore
    """
    with get_conn() as conn:
        return pd.read_sql_query(query, conn)

def fetch_bi_valore_destinazioni() -> pd.DataFrame:
    """Calcola il valore totale (€) delle uscite divise per destinazione"""
    query = """
        SELECT m.destinazione, 
               SUM(m.quantita * p.prezzo_acquisto) as totale_speso
        FROM movimenti m
        JOIN prodotti p ON m.sku = p.sku
        WHERE m.tipo = 'OUT'
        GROUP BY m.destinazione
        ORDER BY totale_speso DESC
    """
    with get_conn() as conn:
        return pd.read_sql_query(query, conn)

def fetch_bi_valore_categorie() -> pd.DataFrame:
    """Calcola il valore totale (€) della merce attualmente in magazzino per categoria"""
    query = """
        SELECT p.categoria, 
               SUM((COALESCE(inv.entrata, 0) - COALESCE(inv.uscita, 0)) * p.prezzo_acquisto) as valore
        FROM prodotti p
        LEFT JOIN (
            SELECT sku,
                   SUM(CASE WHEN tipo = 'IN' THEN quantita ELSE 0 END) as entrata,
                   SUM(CASE WHEN tipo = 'OUT' THEN quantita ELSE 0 END) as uscita
            FROM movimenti
            GROUP BY sku
        ) inv ON p.sku = inv.sku
        GROUP BY p.categoria
        HAVING valore > 0
    """
    with get_conn() as conn:
        return pd.read_sql_query(query, conn)

def fetch_bi_flow_trend() -> pd.DataFrame:
    """Analizza il flusso giornaliero di Entrate vs Uscite per il grafico Trend."""
    query = """
        SELECT data,
               SUM(CASE WHEN tipo='IN' THEN quantita ELSE 0 END) as Entrate,
               SUM(CASE WHEN tipo='OUT' THEN quantita ELSE 0 END) as Uscite
        FROM movimenti 
        GROUP BY data 
        ORDER BY data ASC
    """
    with get_conn() as conn:
        df = pd.read_sql_query(query, conn)
    
    # Trasformazione cruciale per i grafici temporali
    if not df.empty:
        df['data'] = pd.to_datetime(df['data'])
        
    return df

def fetch_destinazioni_lov() -> list:
    with get_conn() as conn:
        # Ora restituisce solo una lista di nomi, non tuple
        return [r['nome'] for r in conn.execute("SELECT nome FROM destinazioni ORDER BY nome").fetchall()]
# ─────────────────────────────────────────────────────────────
# 3. CALLBACK DI SALVATAGGIO
# ─────────────────────────────────────────────────────────────
def salva_fornitore(state: State):
    nome = state.f_nome.strip().upper()
    if not nome:
        notify(state, "warn", "Inserisci un nome!")
        return
    
    try:
        with get_conn() as conn:
            conn.execute("INSERT INTO fornitori (nome) VALUES (?)", (nome,))
            conn.commit()
        
        # AGGIORNAMENTO CRUCIALE:
        nuova_lista = fetch_fornitori_lov() 
        state.fornitori_lov = nuova_lista
        state.prodotto_fornitore_lov = nuova_lista # Se usi questa variabile nel selettore
        
        state.f_nome = ""
        notify(state, "success", f"Fornitore {nome} creato e lista aggiornata!")
        
    except sqlite3.IntegrityError:
        notify(state, "error", "Questo fornitore esiste già!")

def salva_destinazione(state: State):
    nome = state.d_nome.strip().upper()
    if not nome:
        notify(state, "warn", "Inserisci il nome della destinazione.")
        return
    try:
        with get_conn() as conn:
            conn.execute("INSERT INTO destinazioni (nome) VALUES (?)", (nome,))
            conn.commit()
        state.d_nome = ""
        state.destinazioni_lov = fetch_destinazioni_lov()
        notify(state, "success", f"Destinazione '{nome}' salvata!")
    except sqlite3.IntegrityError:
        notify(state, "error", "Questa destinazione esiste già.")

def salva_prodotto(state: State):
    # 1. Recupero dati (ora p_fornitore_id contiene direttamente il NOME)
    sku = str(state.p_sku).strip().upper()
    nome = str(state.p_nome).strip().upper()
    fornitore = state.p_fornitore_id 
    prezzo = float(state.p_prezzo)
    
    # 2. Validazione minima
    if not sku or not nome or not fornitore:
        notify(state, "error", "❌ SKU, Nome e Fornitore sono obbligatori!")
        return

    try:
        with get_conn() as conn:
            # Salviamo direttamente il nome nel campo fornitore
            conn.execute("""
                INSERT INTO prodotti (sku, nome, fornitore, sottoscorta, categoria, prezzo_acquisto) 
                VALUES (?, ?, ?, ?, ?, ?)
            """, (sku, nome, fornitore, int(state.p_sottoscorta), state.p_categoria, prezzo))
            conn.commit()
        
        notify(state, "success", f"✅ Prodotto {sku} salvato correttamente!")
        
        # 3. Reset Campi e Refresh
        state.p_sku = ""
        state.p_nome = ""
        aggiorna_tabelle(state)
        aggiorna_dashboard(state)

    except sqlite3.IntegrityError:
        notify(state, "error", f"⛔ Lo SKU '{sku}' esiste già!")
    except Exception as e:
        notify(state, "error", f"💥 Errore tecnico: {e}")

def salva_movimento(state: State):
    # Funzione di pulizia ultra-aggressiva
    def force_clean(val):
        if isinstance(val, (list, tuple)):
            return val[0] if len(val) >= 0 else None
        return val

    try:
        # Pulizia chirurgica di OGNI parametro
        sku_clean   = force_clean(state.m_sku)
        tipo_clean  = force_clean(state.m_tipo)
        # Forziamo la quantità a intero puro
        qta_clean   = int(force_clean(state.m_qta)) 
        data_clean  = force_clean(state.m_data)
        dest_clean  = force_clean(state.m_destinazione)

        # Validazione
        if not sku_clean:
            notify(state, "error", "Seleziona un prodotto!")
            return
        
        if tipo_clean == "OUT" and not dest_clean:
            notify(state, "warn", "Per le uscite indica una destinazione!")
            return

        try:
            data_oggetto = pd.to_datetime(data_clean)
            data_iso = data_oggetto.strftime('%Y-%m-%d')
        except:
            data_iso = datetime.now().strftime('%Y-%m-%d')

        # 2. --- CONTROLLO GIACENZA (PRIMA DI SALVARE) ---
        if tipo_clean == "OUT":
            with get_conn() as conn:
                res = conn.execute("""
                    SELECT 
                        COALESCE(SUM(CASE WHEN tipo = 'IN' THEN quantita ELSE 0 END), 0) - 
                        COALESCE(SUM(CASE WHEN tipo = 'OUT' THEN quantita ELSE 0 END), 0)
                    FROM movimenti WHERE sku = ?
                """, (sku_clean,)).fetchone()
                
                giacenza_attuale = res[0] if res and res[0] is not None else 0
                
                if qta_clean > giacenza_attuale:
                    notify(state, "error", f"Operazione annullata! Hai solo {giacenza_attuale} pezzi, ne hai richiesti {qta_clean}")
                    return
                
        with get_conn() as conn:
            conn.execute("""
                INSERT INTO movimenti (sku, tipo, quantita, data, destinazione) 
                VALUES (?, ?, ?, ?, ?)
            """, (sku_clean, tipo_clean, qta_clean, data_clean, dest_clean))
            conn.commit()
        

        # Reset stato
        state.m_sku = None
        state.m_destinazione = None
        state.m_qta = 1
        
        aggiorna_tabelle(state)
        aggiorna_dashboard(state)
        notify(state, "success", "Movimento registrato!")

    except ValueError:
        notify(state, "error", "La quantità deve essere un numero valido!")
    except Exception as e:
        print(f"DEBUG ERROR: {type(e).__name__} - {e}") # Controlla il terminale per dettagli
        notify(state, "error", f"Errore: {e}")

# CALLBACK GESTIONE & DASHBOARD
# ─────────────────────────────────────────────────────────────

def on_edit_universale(state: State, var_name: str, payload: dict):
    idx, col, val = payload["index"], payload["col"], payload["value"]
    
    # 1. Recupero DataFrame e Tabella DB
    df = getattr(state, var_name)
    mapping = {"Prodotti": "prodotti", "Movimenti": "movimenti", "Destinazioni": "destinazioni"}
    db_table = mapping.get(state.tipo_ricerca)

    # --- AGGIUNGI QUESTO PEZZO QUI ---
    if var_name == "tbl_ultimi_movimenti":
        db_table = "movimenti"
    # ---------------------------------
    
    if not db_table: return

    # 2. Identificazione Chiave Primaria
    pk_col = "sku" if "sku" in df.columns else ("id" if "id" in df.columns else "nome")
    pk_val = df.iloc[idx][pk_col]

    try:
        # Impedisci la modifica della colonna calcolata 'giacenza'
        if col == "giacenza":
            notify(state, "error", "La giacenza è calcolata dai movimenti, non può essere modificata qui!")
            cerca_dati(state)
            return
        # Dentro on_edit_universale, nella validazione numerica
        if col == "prezzo_acquisto":
            try:
                val = float(val)
                if val < 0: raise ValueError
            except ValueError:
                notify(state, "error", "Prezzo non valido!")
                return
            
        with get_conn() as conn:
            # ... resto del codice
            # --- VALIDAZIONE NUMERICA (ORA ACCETTA 0) ---
            if col in ["quantita", "sottoscorta"]:
                try:
                    val = int(val)
                    if val < 0:  # Blocca solo i numeri negativi, lo 0 passa!
                        notify(state, "error", "Inserisci un numero positivo o zero!")
                        cerca_dati(state)
                        return
                except ValueError:
                    notify(state, "error", "Inserisci un numero valido!")
                    cerca_dati(state)
                    return
            
            # --- CONTROLLO GIACENZA PER USCITE ---
            if db_table == "movimenti" and col == "quantita":
                if var_name == "tbl_ultimi_movimenti":
                    db_table = "movimenti"
                mov = conn.execute("SELECT sku, tipo FROM movimenti WHERE id = ?", (pk_val,)).fetchone()
                if mov and mov['tipo'] == "OUT":
                    # Calcolo giacenza ignorando la riga che stiamo modificando
                    res = conn.execute("""
                        SELECT COALESCE(SUM(CASE WHEN tipo='IN' THEN quantita ELSE -quantita END), 0) 
                        FROM movimenti WHERE sku = ? AND id != ?""", (mov['sku'], pk_val)).fetchone()
                    
                    disponibile = res[0] if res else 0
                    if val > disponibile:
                        notify(state, "error", f"Giacenza insufficiente! Disponibili: {disponibile}")
                        cerca_dati(state)
                        return

            # 3. Esecuzione Update
            conn.execute(f"UPDATE {db_table} SET {col}=? WHERE {pk_col}=?", (val, pk_val))
            conn.commit()

        # 4. Aggiornamento Stato Taipy (Importante per sbloccare la visualizzazione)
        df.loc[idx, col] = val
        setattr(state, var_name, df)
        
        notify(state, "success", "Modifica salvata!")
        aggiorna_tabelle(state)
        aggiorna_dashboard(state)

    except Exception as e:
        notify(state, "error", f"Errore: {e}")
        cerca_dati(state)

def on_delete_table(state: State, var_name: str, payload: dict):
    tbl = getattr(state, var_name)
    idx = payload["index"]
    if var_name == "tbl_movimenti":
        pk_col = "id"
    elif "sku" in tbl.columns:
        pk_col = "sku"
    else:
        pk_col = "nome"
    pk_val = tbl.iloc[idx][pk_col]
    
    table_map = {"tbl_fornitori": "fornitori", "tbl_destinazioni": "destinazioni", 
                 "tbl_prodotti": "prodotti", "tbl_movimenti": "movimenti"}
    try:
        with get_conn() as conn:
            conn.execute(f"DELETE FROM {table_map[var_name]} WHERE {pk_col}=?", (pk_val,))
            conn.commit()
        aggiorna_tabelle(state)
        notify(state, "success", f"🗑️ {var_name.replace('tbl_','').capitalize()} eliminato")
    except Exception as e:
        notify(state, "error", f"⚠️ Elimina fallito: {e}")


def esporta_excel(state: State):
    # 1. Definiamo la cartella di destinazione
    # 'os.getcwd()' restituisce la cartella di lavoro corrente
    cartella_export = os.path.join(BASE_PROJECT_PATH, "exports")    
    # Creiamo la cartella se non esiste ancora
    if not os.path.exists(cartella_export):
        os.makedirs(cartella_export)

    # 2. Prepariamo il nome del file e il percorso completo
    timestamp = datetime.now().strftime('%Y%m%d_%H%M%S')
    filename = f"Report_Magazzino_{timestamp}.xlsx"
    filepath = os.path.join(cartella_export, filename)

    try:
        # 3. Recupero dei dati dal DB
        mov_df = fetch_table_movimenti()
        prod_df = fetch_table_prodotti()
        dest_valore_df = fetch_bi_valore_destinazioni() # La funzione che abbiamo creato prima
        
        stock_query = """
        SELECT p.sku, 
               p.nome, 
               p.categoria,
               p.fornitore,
               p.prezzo_acquisto as "Prezzo Unitario (€)",
               COALESCE(SUM(CASE WHEN m.tipo='IN' THEN m.quantita ELSE -m.quantita END), 0) as giacenza,
               (COALESCE(SUM(CASE WHEN m.tipo='IN' THEN m.quantita ELSE -m.quantita END), 0) * p.prezzo_acquisto) as "Valore Totale (€)"
        FROM prodotti p 
        LEFT JOIN movimenti m ON p.sku = m.sku 
        GROUP BY p.sku
        """
        stock_df = pd.read_sql_query(stock_query, get_conn())

        # 4. Scrittura del file fisicamente nella cartella
        # 4. Scrittura del file
        with pd.ExcelWriter(filepath, engine='xlsxwriter') as writer:
            # Salviamo i fogli
            stock_df.to_excel(writer, sheet_name="Giacenza", index=False) # Ho accorciato il nome per sicurezza
            mov_df.to_excel(writer, sheet_name="Movimenti", index=False)
            prod_df.to_excel(writer, sheet_name="Prodotti", index=False)
            
            # Se abbiamo creato il DataFrame delle destinazioni, salviamo anche quello
            if 'dest_valore_df' in locals():
                dest_valore_df.to_excel(writer, sheet_name="Costi_Destinazione", index=False)
            
            # ACCESSO AI FOGLI PER LA FORMATTAZIONE
            workbook  = writer.book
            # Usa esattamente lo stesso nome messo sopra in sheet_name
            ws_giacenza = writer.sheets['Giacenza']
            
            # Formato Euro
            fmt_euro = workbook.add_format({'num_format': '#,##0.00 €', 'align': 'right'})
            
            # Applichiamo la formattazione alle colonne (E=Prezzo, G=Totale)
            ws_giacenza.set_column('E:E', 15, fmt_euro)
            ws_giacenza.set_column('G:G', 18, fmt_euro)

        # 5. INVIO AL BROWSER (Opzionale, ma utile se vuoi anche il download)
        download(state, filepath, name=filename) 
        
        notify(state, "success", f"✅ Excel salvato in: {cartella_export}")

    except Exception as e:
        print(f"Errore durante la creazione dell'Excel: {e}")
        notify(state, "error", f"Errore: {str(e)}")

def reset_app(state):
    notify(state, "info", "Ricaricamento dati...")
    aggiorna_dashboard(state)
    aggiorna_tabelle(state) # Se hai questa funzione

def esegui_backup(state: State):
    try:
        # 1. Puntiamo alla cartella del progetto corrente
        # 'backups' verrà creata dentro la cartella principale del tuo script
        backup_dir = os.path.join(BASE_PROJECT_PATH, "backups")
        
        # Creiamo la cartella se non esiste
        os.makedirs(backup_dir, exist_ok=True)
        
        # 2. Prepariamo il nome del file con timestamp
        data_ora = datetime.now().strftime("%Y%m%d_%H%M%S")
        nome_file = f"magazzino_backup_{data_ora}.db"
        destinazione = os.path.join(backup_dir, nome_file)
        
        # 3. Esecuzione del backup a caldo (Online Backup)
        with get_conn() as conn:
            # Apriamo la connessione al nuovo file di backup
            with sqlite3.connect(destinazione) as bck:
                conn.backup(bck)
        download(state, destinazione, name=nome_file)
        # 4. Notifica all'utente
        notify(state, "success", f"📦 Backup salvato in: {backup_dir}/{nome_file}")
        print(f"Backup eseguito con successo in: {destinazione}")

    except Exception as e:
        print(f"Errore critico backup: {e}")
        notify(state, "error", f"Errore durante il backup: {e}")
# ─────────────────────────────────────────────────────────────
# 4. INTERFACCIA UTENTE (Markdown Taipy)
# ─────────────────────────────────────────────────────────────

page_data_entry = """
<|part|class_name=dashboard-wrapper|
<|layout|columns=1 1|
<|Magazzino xxx s.r.l. |text|class_name=logo-text|>
<|{ora_attuale}|text|class_name=system-time|>
|>

<hr style="opacity:0.1; margin-bottom: 20px;" />

<|layout|columns=1 1|
# 📦 Data Entry Magazzino
<|Reset|button|on_action=reset_app|class_name=btn-outline|>
|>

<|layout|columns=1 1 1|gap=20px|
<|part|class_name=card-panel|
### 🏢 Anagrafica Fornitore
<|{f_nome}|input|label=Ragione Sociale|class_name=fullwidth|>
<br/>
<|Salva Fornitore|button|on_action=salva_fornitore|class_name=btn-primary|width=100%|>
|>

<|part|class_name=card-panel|
### 📍 Anagrafica Destinazione
<|{d_nome}|input|label=Nome Sede/Cantiere|class_name=fullwidth|>
<br/>
<|Salva Destinazione|button|on_action=salva_destinazione|class_name=btn-primary|width=100%|>
|>

<|part|class_name=card-panel|
### 📦 Nuovo Prodotto
<|{p_sku}|input|label=Codice SKU|class_name=fullwidth|>
<|{p_nome}|input|label=Nome Prodotto|class_name=fullwidth|>
<|{p_categoria}|selector|dropdown|lov={categorie_list}|label=Categoria|class_name=fullwidth|>
<|{p_fornitore_id}|selector|dropdown=True|lov={fornitori_lov}|label=Seleziona Fornitore|filter=True|class_name=fullwidth|>

<|layout|columns=1 1|gap=10px|
<|{p_sottoscorta}|number|label=Soglia Sottoscorta|class_name=fullwidth|>
<|{p_prezzo}|number|label=Prezzo Acquisto (€)|class_name=fullwidth|>
|>

<br/>
<|Salva Prodotto|button|on_action=salva_prodotto|class_name=btn-primary|width=100%|>
|>
|>

<|part|class_name=card-panel|
### 🔄 Registrazione Movimento Merci
<|layout|columns=1 1 1|gap=20px|
<|part|
**Prodotto**
<|{m_sku}|selector|dropdown=True|lov={prodotti_lov}|label=Seleziona Prodotto|value_by_id=True|filter=True|class_name=fullwidth|>
**Quantità**
<|{m_qta}|number|label=Q.tà|min=1|class_name=fullwidth|>
|>
<|part|
**Tipo Operazione**
<|{m_tipo}|selector|lov={[('IN', '📥 ENTRATA'), ('OUT', '📤 USCITA')]}|dropdown=False|class_name=segmented-control|>
**Data Operazione**
<|{m_data}|date|label=Seleziona Data|class_name=fullwidth|>
|>
<|part|
**Destinazione (per Uscite)**
<|{m_destinazione}|selector|dropdown=True|lov={destinazioni_lov}|label=Seleziona Destinazione|value_by_id=True|filter=True|class_name=fullwidth|>
<|Registra Movimento|button|on_action=salva_movimento|class_name=btn-primary|width=100%|>
|>
|>
|>
|>
"""
page_gestione = """
<|part|class_name=dashboard-wrapper|
<|layout|columns=1 1|
<|Magazzino xxx s.r.l. |text|class_name=logo-text|>

<|{ora_attuale}|text|class_name=system-time|>
|>

# 📦 Gestione Database

### 🛠️ Azioni Rapide
<|layout|columns=1 1 1|gap=30px|
<|part|class_name=card-panel|
**Report Mensile**
<br/><br/>
<|ESPORTA EXCEL|button|on_action=esporta_excel|class_name=btn-primary|width=100%|>
|>

<|part|class_name=card-panel|
**Aggiornamento**
<br/><br/>
<|SINCRONIZZA TABELLE|button|on_action=aggiorna_tabelle|class_name=btn-primary|width=100%|>
|>

<|part|class_name=card-panel|
**Sicurezza**
<br/><br/>
<|🛡️ BACKUP DB|button|on_action=esegui_backup|class_name=btn-primary|width=100%|>
|>
|>

<br/>

<|part|class_name=card-panel|
### 🔎 Console di Controllo
<|layout|columns=1 1 2 1|gap=15px|
<|{tipo_ricerca}|selector|lov={["Prodotti", "Movimenti", "Destinazioni"]}|dropdown=True|label=Cosa Gestire|on_change=reset_ricerca|>

<|{filtro_rapido}|selector|lov={filtri_lov}|dropdown=True|label=Filtro Stato|on_change=cerca_dati|>

<|{search_query}|input|label=Cerca SKU o Nome...|on_action=cerca_dati|class_name=fullwidth|>

<|part|class_name=align-end|
<|CERCA|button|on_action=cerca_dati|class_name=btn-primary|width=100%|>
|>
|>

<|PULISCI TUTTO|button|on_action=reset_ricerca|class_name=btn-primary|>
|>

<br/>

<|part|class_name=card-panel|
### 📝 Risultati
<|{tbl_risultati}|table|editable=True|on_edit=on_edit_universale|filter=True|width=100%|page_size=10|rebuild={rebuild_tabella}|on_delete=on_delete_table|>
|>

<br/>

<|part|class_name=card-panel|
### 🕒 Ultimi 5 Movimenti Registrati
<|{tbl_ultimi_movimenti}|table|editable=True|on_edit=on_edit_universale|filter=False|width=100%|rebuild={rebuild_tabella}|>
|>
|>
"""

page_dashboard = """
<|part|class_name=dashboard-wrapper|
<|layout|columns=1 1|
<|Magazzino xxx s.r.l. |text|class_name=logo-text|>
<|{ora_attuale}|text|class_name=system-time|>
|>

<hr style="opacity:0.1; margin-bottom: 25px;" />

<|layout|columns=1 1|
# 📈 Performance & Monitoraggio
<|Aggiorna Dati|button|on_action=aggiorna_dashboard|class_name=btn-primary|>
|>

<|layout|columns=1 1 1|gap=20px|

<|part|class_name=kpi-card|
<|USCITE TOTALI (MESE)|text|class_name=text-secondary|>
<br/>
##<|{kpi_total_movimenti}|text|style=color:#ef4444;font-size:2rem;font-weight:bold|>
|>
<|part|class_name=kpi-card kpi-card--alert|
<|CRITICITÀ STOCK|text|class_name=text-danger|>
<br/>
##<|{kpi_understock}|text|style=color:#ef4444;font-size:2rem;font-weight:bold|>
|>
<|part|class_name=kpi-card|
<|TOP PARTNER|text|class_name=text-secondary|>
<br/>
## <|{kpi_top_supplier}|text|>
|>
|>

<br/>

<|layout|columns=1 1|gap=25px|
<|part|class_name=card-panel|
### 📍 Allocazione Costi per Destinazione (€)
<|{chart_valore_dest}|chart|type=bar|x=destinazione|y=totale_speso|color=#3b82f6|>
|>

<|part|class_name=card-panel|
### 💰 Valore Immobilizzato per Categoria (€)
<|{chart_valore_cat}|chart|type=bar|x=categoria|y=valore|color=#22c55e|>
|>
|>

<br/>

<|layout|columns=1|
<|part|class_name=card-panel|
### 📈 Trend Operativo (Entrate vs Uscite)
<|{chart_flow_trend}|chart|type=area|x=data|y[1]=Entrate|y[2]=Uscite|color[1]=#10b981|color[2]=#ef4444|width=100%|>
|>
|>

|>
"""
# ─────────────────────────────────────────────────────────────
# 5. AVVIO APPLICAZIONE (VERSIONE ENTERPRISE)
# ─────────────────────────────────────────────────────────────

if __name__ == "__main__":
    init_db()
    # 1. Caricamento LOV (Dropdown)
    fornitori_lov = fetch_fornitori_lov()
    categorie_lov = categorie_list
    prodotto_fornitore_lov = fornitori_lov
    prodotti_lov = fetch_prodotti_lov()
    destinazioni_lov = fetch_destinazioni_lov()
    
    # 2. Caricamento Tabelle (Gestione)
    tbl_fornitori = fetch_table_fornitori()
    tbl_destinazioni = fetch_table_destinazioni()
    tbl_prodotti = fetch_table_prodotti()
    tbl_movimenti = fetch_table_movimenti()
    tbl_ultimi_movimenti = fetch_ultimi_5_movimenti()

    # 3. CRUCIALE: Popolamento Variabili Dashboard prima del run
    # Se non fai questo, la dashboard resta vuota/brutta al primo avvio
    kpi_valore_uscite = fetch_kpi_valore_uscite_mese()
    kpi_total_movimenti = fetch_kpi_total_movimenti()
    kpi_understock = fetch_kpi_understock()
    kpi_top_supplier = fetch_kpi_top_supplier()
    
    chart_valore_dest = fetch_bi_valore_destinazioni()
    chart_valore_cat = fetch_bi_valore_categorie()
    chart_flow_trend = fetch_bi_flow_trend()
    chart_supplier_dist = fetch_bi_supplier_dist()

    #creazione indici per velocità sql

    def create_indexes():
        with get_conn() as conn:
            # Crea indici sulle colonne che usi per cercare e filtrare
            conn.execute("CREATE INDEX IF NOT EXISTS idx_prodotti_sku_nome ON prodotti(sku, nome)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_movimenti_sku ON movimenti(sku)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_movimenti_data ON movimenti(data)")
            conn.commit()

    # 4. Configurazione Pagine
    all_pages = {
        "/": "<|navbar|>", 
        "Data_Entry": page_data_entry,
        "Gestione": page_gestione,
        "Dashboard": page_dashboard
    }

    print("✅ Database pronto. Interfaccia professionale in caricamento...")
    
    # 5. Avvio con CSS integrato
    Gui(pages=all_pages).run(
        title="Magazzino xxx",
        use_restyle=True,
        port=5001,
        run_browser=True,
        reload=True,
        dev_mode=True
    )
