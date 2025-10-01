# app.py
import streamlit as st
import sqlite3
import pandas as pd
from datetime import date
from streamlit_calendar import calendar
import smtplib
from email.mime.text import MIMEText
import io, os, base64, secrets, hashlib

# ==========================================
# 0) CONFIGURACIÓN BÁSICA
# ==========================================
st.set_page_config(page_title="Vacaciones Tesorería", layout="wide")

USUARIOS = ["Magali Rupay", "Luz Granara", "Liz Samaniego", "Jhoset Rueda", "Sergio Salazar"]
APROBADORES = ["Luz Granara", "Sergio Salazar"]   # estos roles ven el panel sin clave extra

# TODOS inician con 30 días
GANADOS_INICIALES = {u: 30 for u in USUARIOS}

FECHAS_INGRESO = {
    "Magali Rupay": "2008-01-01",
    "Luz Granara": "1993-09-16",
    "Liz Samaniego": "2016-10-01",
    "Jhoset Rueda": "2016-10-01",
    "Sergio Salazar": "2025-04-01",
}

EMAILS = {
    "Magali Rupay": "mrm@limatours.com.pe",
    "Luz Granara": "agc@limatours.com.pe",
    "Liz Samaniego": "lst@limatours.com.pe",
    "Jhoset Rueda": "jra@limatours.com.pe",
    "Sergio Salazar": "sjs@limatours.com.pe",
}

# --- SendGrid (dejado tal cual a tu pedido) ---
SMTP_SERVER = "smtp.sendgrid.net"
SMTP_PORT = 587
REMITENTE = "sjs@limatours.com.pe"
SMTP_USER = "apikey"
SMTP_PASS = "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"

HOY = date.today()

# ==========================================
# 1) BD / ESQUEMA
# ==========================================
@st.cache_resource
def get_conn():
    return sqlite3.connect("vacaciones.db", check_same_thread=False)

conn = get_conn()
c = conn.cursor()

# Tabla de vacaciones
c.execute("""
CREATE TABLE IF NOT EXISTS vacaciones (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    usuario TEXT,
    fecha_inicio TEXT,
    fecha_fin TEXT,
    dias INTEGER,
    comentario TEXT,
    estado TEXT DEFAULT 'Pendiente',    -- Pendiente | Aprobado | Rechazado
    usado_ganado REAL DEFAULT 0,
    usado_proporcional REAL DEFAULT 0,
    advertencia TEXT DEFAULT ''
)
""")

# Tabla de saldos
c.execute("""
CREATE TABLE IF NOT EXISTS dias_vacaciones (
    usuario TEXT PRIMARY KEY,
    dias_ganados REAL,
    dias_usados REAL DEFAULT 0,
    fecha_ingreso TEXT,
    ultimo_aniversario TEXT,
    prop_congelados REAL DEFAULT 0,
    prop_usados REAL DEFAULT 0
)
""")
conn.commit()

# Tabla de usuarios (login)
c.execute("""
CREATE TABLE IF NOT EXISTS usuarios (
    usuario TEXT PRIMARY KEY,
    email TEXT,
    rol TEXT,                   -- 'APROBADOR' o 'USUARIO'
    salt TEXT,                  -- base64
    pwd_hash TEXT               -- base64 (pbkdf2_hmac sha256)
)
""")
conn.commit()

def ensure_schema():
    # Columna dias_asignados en saldos (solo si no existe)
    c.execute("PRAGMA table_info(dias_vacaciones)")
    cols = [r[1] for r in c.fetchall()]
    if "dias_asignados" not in cols:
        c.execute("ALTER TABLE dias_vacaciones ADD COLUMN dias_asignados REAL DEFAULT 0")
        conn.commit()

ensure_schema()

def calcular_ultimo_aniversario(f_ing: date, hoy: date) -> date:
    ult = date(hoy.year, f_ing.month, f_ing.day)
    if ult > hoy:
        ult = date(hoy.year - 1, f_ing.month, f_ing.day)
    return ult

def seed_saldos_y_usuarios():
    # Saldos
    for u in USUARIOS:
        c.execute("SELECT usuario FROM dias_vacaciones WHERE usuario=?", (u,))
        row = c.fetchone()
        f_ing = pd.to_datetime(FECHAS_INGRESO[u]).date()
        ult = calcular_ultimo_aniversario(f_ing, HOY)
        if not row:
            c.execute("""
                INSERT INTO dias_vacaciones
                (usuario, dias_ganados, dias_usados, fecha_ingreso, ultimo_aniversario, prop_congelados, prop_usados, dias_asignados)
                VALUES (?,?,?,?,?,?,?,?)
            """, (u, GANADOS_INICIALES[u], 0, FECHAS_INGRESO[u], str(ult), 0, 0, GANADOS_INICIALES[u]))
        else:
            c.execute("UPDATE dias_vacaciones SET dias_asignados = COALESCE(dias_asignados, ?), dias_ganados = COALESCE(dias_ganados, ?) WHERE usuario=?",
                      (GANADOS_INICIALES[u], GANADOS_INICIALES[u], u))
    conn.commit()

    # Usuarios
    for u in USUARIOS:
        rol = "APROBADOR" if u in APROBADORES else "USUARIO"
        c.execute("SELECT usuario FROM usuarios WHERE usuario=?", (u,))
        if not c.fetchone():
            c.execute("INSERT INTO usuarios (usuario, email, rol, salt, pwd_hash) VALUES (?,?,?,?,?)",
                      (u, EMAILS[u], rol, None, None))
    conn.commit()

seed_saldos_y_usuarios()

def ensure_user_in_saldos(u):
    c.execute("SELECT usuario FROM dias_vacaciones WHERE usuario=?", (u,))
    if not c.fetchone():
        f_ing = pd.to_datetime(FECHAS_INGRESO[u]).date()
        ult = calcular_ultimo_aniversario(f_ing, HOY)
        c.execute("""
            INSERT INTO dias_vacaciones 
            (usuario, dias_ganados, dias_usados, fecha_ingreso, ultimo_aniversario, prop_congelados, prop_usados, dias_asignados)
            VALUES (?,?,?,?,?,?,?,?)
        """, (u, GANADOS_INICIALES[u], 0, FECHAS_INGRESO[u], str(ult), 0, 0, GANADOS_INICIALES[u]))
        conn.commit()

for _u in USUARIOS:
    ensure_user_in_saldos(_u)

# ==========================================
# 2) UTILIDADES & AUTH
# ==========================================
def ddmmyyyy(dt: str | date) -> str:
    d = pd.to_datetime(dt).date()
    return d.strftime("%d/%m/%Y")

def send_email(to_name, subject, body):
    to_addr = EMAILS.get(to_name)
    if not to_addr:
        return
    msg = MIMEText(body)
    msg["Subject"] = subject
    msg["From"] = REMITENTE
    msg["To"] = to_addr
    try:
        with smtplib.SMTP(SMTP_SERVER, SMTP_PORT) as server:
            server.starttls()
            server.login(SMTP_USER, SMTP_PASS)
            server.send_message(msg)
    except Exception as e:
        print("Error al enviar correo:", e)

def notify_approvers_new_request(usuario, fi, ff, dias, comentario):
    # envía aviso a cada aprobador
    for apr in APROBADORES:
        body = (f"Hola {apr},\n\n"
                f"El colaborador **{usuario}** ha registrado una solicitud de vacaciones:\n"
                f"- Desde: {fi}\n- Hasta: {ff}\n- Días: {dias}\n"
                f"- Comentario: {comentario or '—'}\n\n"
                f"Por favor, ingresa a la plataforma para **aprobar o rechazar**.\n\nTesorería")
        send_email(apr, "📝 Nueva solicitud de vacaciones", body)

def hash_password(password: str, salt_b: bytes | None = None):
    if salt_b is None:
        salt_b = os.urandom(16)
    dk = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt_b, 100_000)
    return base64.b64encode(salt_b).decode(), base64.b64encode(dk).decode()

def verify_password(password: str, salt_b64: str, hash_b64: str) -> bool:
    salt_b = base64.b64decode(salt_b64)
    _, new_hash = hash_password(password, salt_b)
    return secrets.compare_digest(new_hash, hash_b64)

def iniciar_sesion(nombre: str, password: str):
    c.execute("SELECT salt, pwd_hash, rol FROM usuarios WHERE usuario=?", (nombre,))
    row = c.fetchone()
    if not row:
        return False, None
    salt, pwd_hash, rol = row
    if salt is None or pwd_hash is None:
        return "FIRST", rol
    ok = verify_password(password, salt, pwd_hash)
    return ok, rol if ok else (False, None)

def crear_clave(nombre: str, password: str):
    salt, pwd_hash = hash_password(password)
    c.execute("UPDATE usuarios SET salt=?, pwd_hash=? WHERE usuario=?", (salt, pwd_hash, nombre))
    conn.commit()

def cambiar_clave(nombre: str, actual: str, nueva: str):
    c.execute("SELECT salt, pwd_hash FROM usuarios WHERE usuario=?", (nombre,))
    row = c.fetchone()
    if not row or row[0] is None:
        return False
    if not verify_password(actual, row[0], row[1]):
        return False
    salt, pwd_hash = hash_password(nueva)
    c.execute("UPDATE usuarios SET salt=?, pwd_hash=? WHERE usuario=?", (salt, pwd_hash, nombre))
    conn.commit()
    return True

def recuperar_clave(nombre: str):
    temp = secrets.token_urlsafe(8)  # clave temporal
    salt, pwd_hash = hash_password(temp)
    c.execute("UPDATE usuarios SET salt=?, pwd_hash=? WHERE usuario=?", (salt, pwd_hash, nombre))
    conn.commit()
    send_email(
        nombre,
        "🔐 Recuperación de contraseña - Vacaciones Tesorería",
        f"Hola {nombre},\n\nTu contraseña temporal es: {temp}\n"
        f"Ingresa y cámbiala desde el botón 'Cambiar contraseña'.\n\nTesorería"
    )

# ==========================================
# 3) LÓGICA DE VACACIONES
# ==========================================
def aprobar_solicitud(row, motivo=""):
    fi = str(row["fecha_inicio"]); ff = str(row["fecha_fin"]); dias = int(row["dias"])
    # Verifica saldo actual
    c.execute("SELECT dias_asignados, dias_usados FROM dias_vacaciones WHERE usuario=?", (row["usuario"],))
    asignados, usados = c.fetchone()
    saldo = float(asignados) - float(usados)
    if dias > saldo:
        st.error(f"No se puede aprobar. {row['usuario']} no tiene saldo suficiente ({saldo:.1f} días).")
        return
    c.execute("UPDATE vacaciones SET estado=?, comentario=? WHERE id=?", ("Aprobado", motivo or row.get("comentario", ""), int(row["id"])))
    c.execute("UPDATE dias_vacaciones SET dias_usados = dias_usados + ? WHERE usuario=?", (dias, row["usuario"]))
    conn.commit()
    send_email(row["usuario"], "✅ Vacaciones aprobadas",
               f"Hola {row['usuario']},\n\nTu solicitud del {fi} al {ff} ha sido APROBADA.\n\n¡Disfruta!\n\nTesorería")

def rechazar_solicitud(row, motivo_rechazo: str):
    if not motivo_rechazo:
        st.error("Debes ingresar un motivo para rechazar.")
        return
    c.execute("UPDATE vacaciones SET estado='Rechazado', comentario=? WHERE id=?", (motivo_rechazo, int(row["id"])))
    conn.commit()
    send_email(row["usuario"], "❌ Vacaciones rechazadas",
               f"Hola {row['usuario']},\n\nTu solicitud del {row['fecha_inicio']} al {row['fecha_fin']} "
               f"ha sido RECHAZADA.\nMotivo: {motivo_rechazo}\n\nTesorería")

def eliminar_solicitud(row, motivo="Eliminado por aprobador"):
    if str(row["estado"]) == "Aprobado":
        c.execute("UPDATE dias_vacaciones SET dias_usados = MAX(0, dias_usados - ?) WHERE usuario=?", (int(row["dias"]), row["usuario"]))
        conn.commit()
    c.execute("DELETE FROM vacaciones WHERE id=?", (int(row["id"]),))
    conn.commit()
    send_email(row["usuario"], "🗑️ Solicitud eliminada",
               f"Hola {row['usuario']},\n\nTu registro del {row['fecha_inicio']} al {row['fecha_fin']} "
               f"ha sido ELIMINADO por Tesorería.\nMotivo: {motivo}\n")

# ==========================================
# 4) LOGIN CENTRADO
# ==========================================
if "user" not in st.session_state:
    st.session_state.user = None
if "rol" not in st.session_state:
    st.session_state.rol = None

st.markdown("<h1 style='text-align:center;color:#2E86C1'>📅 Vacaciones - Tesorería</h1>", unsafe_allow_html=True)

if st.session_state.user is None:
    st.markdown("### 🔐 Acceso")
    modo = st.radio("Acción", ["Iniciar sesión", "Crear contraseña (1ra vez)", "Recuperar contraseña", "Cambiar contraseña"],
                    index=0, horizontal=True)
    sel_user = st.selectbox("Usuario", USUARIOS)

    if modo == "Iniciar sesión":
        pwd = st.text_input("Contraseña", type="password")
        if st.button("Entrar", use_container_width=True):
            ok, rol = iniciar_sesion(sel_user, pwd)
            if ok == "FIRST":
                st.warning("Este usuario aún no tiene contraseña. Usa 'Crear contraseña (1ra vez)'.")
            elif ok:
                st.session_state.user = sel_user
                st.session_state.rol = rol
                st.success(f"Bienvenido, {sel_user}")
                st.rerun()
            else:
                st.error("Usuario o contraseña inválidos.")

    elif modo == "Crear contraseña (1ra vez)":
        p1 = st.text_input("Nueva contraseña", type="password")
        p2 = st.text_input("Repite contraseña", type="password")
        if st.button("Crear/Resetear", use_container_width=True):
            if p1 and p1 == p2:
                crear_clave(sel_user, p1)
                st.success("Contraseña creada/actualizada. Ahora puedes iniciar sesión.")
            else:
                st.error("Las contraseñas no coinciden.")

    elif modo == "Recuperar contraseña":
        if st.button("Enviar clave temporal", use_container_width=True):
            recuperar_clave(sel_user)
            st.success("Se envió una contraseña temporal a tu correo.")

    elif modo == "Cambiar contraseña":
        st.info("Primero inicia sesión para cambiar tu contraseña.")

    st.stop()

# Ya con sesión
usuario = st.session_state.user
es_aprobador = (st.session_state.rol == "APROBADOR")
st.caption(f"🧑 Usuario: **{usuario}** | Rol: **{st.session_state.rol}**")
if st.button("Cerrar sesión"):
    st.session_state.user = None
    st.session_state.rol = None
    st.rerun()

# ==========================================
# 5) SALDOS, SOLICITUDES, CALENDARIO, EXPORT
# ==========================================
# --- SALDOS ---
st.markdown("### 📊 Resumen de días")
c.execute("SELECT dias_asignados, dias_usados, ultimo_aniversario FROM dias_vacaciones WHERE usuario=?", (usuario,))
row_saldo = c.fetchone()
if row_saldo is None:
    st.error("Usuario no inicializado en saldos.")
    st.stop()
asignados, usados, ult = row_saldo
saldo = float(asignados) - float(usados)

col1, col2, col3 = st.columns(3)
col1.metric("Asignados", f"{asignados:.1f}")
col2.metric("Usados", f"{usados:.1f}")
col3.metric("Saldo", f"{saldo:.1f}")

f_ing = pd.to_datetime(FECHAS_INGRESO[usuario]).date()
prox = date(pd.to_datetime(ult).year + 1, f_ing.month, f_ing.day)
st.caption(f"📌 Último aniversario: {ddmmyyyy(ult)} | Próximo: {ddmmyyyy(prox)}")
st.divider()

# --- NUEVA SOLICITUD ---
st.markdown("### ✍️ Registrar nueva solicitud")
colA, colB = st.columns(2)
fi = colA.date_input("Fecha inicio", min_value=HOY)
ff = colB.date_input("Fecha fin", min_value=fi)
com = st.text_area("Comentario (opcional)")

if st.button("Enviar solicitud", use_container_width=True):
    dias = (ff - fi).days + 1
    # saldo en tiempo real
    c.execute("SELECT dias_asignados, dias_usados FROM dias_vacaciones WHERE usuario=?", (usuario,))
    asignados_v, usados_v = c.fetchone()
    saldo_v = float(asignados_v) - float(usados_v)
    # solapamiento (pendiente y aprobado)
    c.execute("""
        SELECT 1 FROM vacaciones
        WHERE usuario=? AND estado IN ('Pendiente','Aprobado') AND (
            (date(fecha_inicio) <= date(?) AND date(fecha_fin) >= date(?)) OR
            (date(fecha_inicio) <= date(?) AND date(fecha_fin) >= date(?)) OR
            (date(fecha_inicio) >= date(?) AND date(fecha_fin) <= date(?))
        )
    """, (usuario, fi, fi, ff, ff, fi, ff))
    solapado = c.fetchone()
    if dias <= 0:
        st.error("Rango inválido.")
    elif dias > saldo_v:
        st.error(f"No puedes solicitar {dias} días. Saldo disponible: {saldo_v:.1f}.")
    elif solapado:
        st.error("Las fechas se solapan con otra solicitud (Pendiente/Aprobada).")
    else:
        c.execute("""INSERT INTO vacaciones (usuario, fecha_inicio, fecha_fin, dias, comentario)
                     VALUES (?,?,?,?,?)""",
                  (usuario, fi.strftime("%Y-%m-%d"), ff.strftime("%Y-%m-%d"), dias, com))
        conn.commit()
        # notificar a aprobadores
        notify_approvers_new_request(usuario, fi.strftime("%d/%m/%Y"), ff.strftime("%d/%m/%Y"), dias, com)
        st.success(f"Solicitud registrada ({dias} días). Se notificó a los aprobadores.")
        st.rerun()

st.divider()

# --- LISTA ---
st.markdown("### 📋 Solicitudes registradas")
df_list = pd.read_sql("SELECT * FROM vacaciones ORDER BY date(fecha_inicio) DESC", conn)
if not df_list.empty:
    df_show = df_list.copy()
    df_show["fecha_inicio"] = pd.to_datetime(df_show["fecha_inicio"]).dt.strftime("%d/%m/%Y")
    df_show["fecha_fin"] = pd.to_datetime(df_show["fecha_fin"]).dt.strftime("%d/%m/%Y")
    for _, r in df_show.iterrows():
        with st.container():
            c1, c2 = st.columns([4,1])
            c1.write(f"{r['usuario']} → {r['fecha_inicio']} a {r['fecha_fin']} | {r['dias']} días | Estado: **{r['estado']}**")
            if r["estado"] == "Pendiente" and r["usuario"] == usuario:
                if c2.button("❌ Cancelar", key=f"cancel{r['id']}"):
                    c.execute("DELETE FROM vacaciones WHERE id=?", (int(r["id"]),))
                    conn.commit()
                    st.success("Solicitud cancelada.")
                    st.rerun()
else:
    st.info("No hay solicitudes.")

# --- CALENDARIO ---
st.markdown("### 📆 Calendario")
if not df_list.empty:
    events = []
    for _, r in df_list.iterrows():
        color = "#3498DB"  # pendiente
        if r["estado"] == "Aprobado": color = "#27AE60"
        if r["estado"] == "Rechazado": color = "#E74C3C"
        events.append({
            "title": f"{r['usuario']} ({r['estado']})",
            "start": pd.to_datetime(r["fecha_inicio"]).strftime("%Y-%m-%d"),
            "end": (pd.to_datetime(r["fecha_fin"]) + pd.Timedelta(days=1)).strftime("%Y-%m-%d"),
            "color": color
        })
    calendar(events=events, options={"initialView": "dayGridMonth", "locale": "es", "height": 600})

# --- EXPORTAR EXCEL ---
st.markdown("### 📥 Exportar registros a Excel")
df_export = pd.read_sql("SELECT usuario, fecha_inicio, fecha_fin, dias, estado FROM vacaciones ORDER BY date(fecha_inicio) DESC", conn)
if not df_export.empty:
    df_exp = df_export.rename(columns={
        "usuario": "Nombre (solicitante)",
        "fecha_inicio": "Fecha Inicio",
        "fecha_fin": "Fecha Regreso",
        "dias": "Días usados",
        "estado": "Estado"
    })
    output = io.BytesIO()
    with pd.ExcelWriter(output, engine="openpyxl") as writer:
        df_exp.to_excel(writer, index=False, sheet_name="Vacaciones")
    st.download_button("⬇️ Descargar Excel", data=output.getvalue(),
                       file_name="vacaciones_tesoreria.xlsx",
                       mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")
else:
    st.info("No hay registros para exportar.")

# ==========================================
# 6) PANEL APROBADORES (SIN CLAVE EXTRA)
# ==========================================
if es_aprobador:
    st.markdown("### 🔑 Panel de Aprobación")

    # Pendientes
    st.markdown("#### Pendientes")
    dfp = pd.read_sql("SELECT * FROM vacaciones WHERE estado='Pendiente' ORDER BY date(fecha_inicio) ASC", conn)
    if dfp.empty:
        st.info("No hay pendientes.")
    for _, r in dfp.iterrows():
        with st.container():
            cA, cB, cC = st.columns([3,1,2])
            cA.info(f"{r['usuario']} → {r['fecha_inicio']} a {r['fecha_fin']} ({r['dias']} días)")
            motivo_ap = cC.text_input("Motivo (opcional al aprobar / obligatorio al rechazar)", key=f"m{r['id']}")
            if cB.button("✅ Aprobar", key=f"ok{r['id']}"):
                aprobar_solicitud(r, motivo_ap)
                st.rerun()
            if cB.button("❌ Rechazar", key=f"no{r['id']}"):
                rechazar_solicitud(r, motivo_ap)
                st.rerun()

    # Configuración de saldos
    st.markdown("#### 🧮 Configurar días asignados")
    conf = pd.read_sql("SELECT usuario, dias_asignados, dias_usados FROM dias_vacaciones ORDER BY usuario", conn)
    for _, rr in conf.iterrows():
        with st.container():
            d1, d2, d3, d4 = st.columns([3,1.5,1,1])
            d1.write(f"**{rr['usuario']}**  | Usados: {rr['dias_usados']:.1f}")
            nuevo_asign = d2.number_input("Asignados", min_value=0.0, step=1.0, value=float(rr["dias_asignados"]), key=f"a_{rr['usuario']}")
            if d3.button("💾 Guardar", key=f"ga_{rr['usuario']}"):
                c.execute("UPDATE dias_vacaciones SET dias_asignados=? WHERE usuario=?", (nuevo_asign, rr["usuario"]))
                conn.commit()
                st.success("Asignados actualizados.")
                st.rerun()
            if d4.button("↩️ Reset usados", key=f"ru_{rr['usuario']}"):
                c.execute("UPDATE dias_vacaciones SET dias_usados=0 WHERE usuario=?", (rr["usuario"],))
                conn.commit()
                st.success("Usados reiniciados.")
                st.rerun()

    # Administración
    st.markdown("#### 🗃️ Administración de registros")
    dfa = pd.read_sql("SELECT * FROM vacaciones ORDER BY date(fecha_inicio) DESC", conn)
    if dfa.empty:
        st.info("No hay registros.")
    else:
        for _, r in dfa.iterrows():
            with st.container():
                e1, e2, e3, e4 = st.columns([4,1.5,1,1])
                e1.write(f"{r['usuario']} → {r['fecha_inicio']} a {r['fecha_fin']} | {r['dias']} días | Estado: **{r['estado']}**")
                motivo = e2.text_input("Motivo", key=f"mot{r['id']}", placeholder="opcional")
                if e3.button("🗑️ Eliminar", key=f"del{r['id']}"):
                    eliminar_solicitud(r, motivo or "Eliminado por aprobador")
                    st.success("Registro eliminado.")
                    st.rerun()
                if r["estado"] == "Aprobado" and e4.button("↩️ Revertir a Pendiente", key=f"rev{r['id']}"):
                    c.execute("UPDATE dias_vacaciones SET dias_usados = MAX(0, dias_usados - ?) WHERE usuario=?", (int(r["dias"]), r["usuario"]))
                    c.execute("UPDATE vacaciones SET estado='Pendiente' WHERE id=?", (int(r["id"]),))
                    conn.commit()
                    st.success("Estado revertido a Pendiente.")
                    st.rerun()
