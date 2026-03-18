# ✈️ Bot de Telegram para seguimiento de vuelos

Bot de grupo para registrar vuelos y recibir notificaciones automáticas cuando
su estado cambia (retraso, despegue, aterrizaje, cancelación…).

---

## Cómo funciona

1. Alguien en el grupo escribe `/registrarvuelo EY104`.
2. El bot consulta la API de Airlabs y muestra la info inicial del vuelo.
3. Desde **3 horas antes** de la salida programada hasta **1 hora después**,
   el bot comprueba el estado cada **15 minutos**.
4. Si hay un cambio (estado o retraso ≥ 5 min), manda una notificación al grupo.
5. Cuando el vuelo aterriza o se cancela, se desactiva automáticamente.

---

## Requisitos previos

- **Python 3.10 o superior**
- Cuenta en [Airlabs](https://airlabs.co) (plan Free: **1 000 requests/mes**)
- Un bot de Telegram creado con [@BotFather](https://t.me/BotFather)

---

## Instalación y configuración

### 1. Instalar dependencias

```bash
pip install -r requirements.txt
```

### 2. Crear el bot en Telegram

1. Abre Telegram y busca **@BotFather**.
2. Escribe `/newbot` y sigue las instrucciones.
3. Copia el **token** que te da al final (tiene esta pinta: `123456:ABC-DEF...`).

> **Importante:** Para que el bot pueda leer mensajes en grupos, desactiva el
> modo privado: en BotFather escribe `/setprivacy` → selecciona tu bot → `Disable`.

### 3. Obtener la API key de Airlabs

1. Regístrate en <https://airlabs.co>.
2. En tu panel, copia la **API Key** del plan Free.
3. El plan gratuito incluye **1 000 requests/mes**, más que suficiente para un
   grupo pequeño (cada vuelo monitorizado consume ~16 requests en la ventana de 4 h).

### 4. Configurar el archivo `.env`

```bash
cp .env.example .env
```

Edita `.env` y rellena:

```env
TELEGRAM_TOKEN=123456:ABC-DEF...
AIRLABS_API_KEY=tu_api_key_de_airlabs
```

### 5. Ejecutar el bot

```bash
python boy.py
```

Verás algo como:

```
2026-03-18 10:00:00  [INFO]  🤖  Bot arrancado. Intervalo de monitorización: 900 s.
```

---

## Comandos disponibles

| Comando | Descripción |
|---|---|
| `/registrarvuelo EY104` | Registra el vuelo EY104 para seguimiento |
| `/eliminarvuelo EY104` | Deja de seguir el vuelo EY104 |
| `/vuelos` | Lista todos los vuelos activos en este grupo |
| `/estado EY104` | Consulta el estado actual del vuelo EY104 |
| `/start` o `/ayuda` | Muestra la ayuda |

---

## Añadir el bot al grupo

1. En Telegram, abre tu grupo → **Añadir miembro** → busca el nombre de tu bot.
2. Dale permisos de **enviar mensajes**.
3. Escribe `/start` en el grupo para verificar que responde.

---

## Ejecutar en segundo plano (Windows)

### Opción A — Minimizado en una terminal

Simplemente deja la ventana de PowerShell/CMD abierta mientras el equipo esté
encendido.

### Opción B — Tarea programada de Windows

1. Abre el **Programador de tareas** (`taskschd.msc`).
2. Crea una tarea básica → Desencadenador: **Al iniciar sesión**.
3. Acción: **Iniciar un programa** → `python` → Argumentos: ruta completa a `boy.py`.

### Opción C — Servidor o VPS (recomendado para 24/7)

Plataformas **gratuitas** donde puedes hospedar el bot:

- [Railway](https://railway.app) — plan Hobby gratuito, muy fácil
- [Render](https://render.com) — plan Free (se duerme si no hay tráfico; no ideal)
- [Oracle Cloud Free Tier](https://www.oracle.com/cloud/free/) — VM Always Free

En Linux, crea un servicio systemd:

```ini
# /etc/systemd/system/bot-vuelos.service
[Unit]
Description=Bot de vuelos Telegram

[Service]
ExecStart=/usr/bin/python3 /ruta/a/boy.py
Restart=always
WorkingDirectory=/ruta/a/

[Install]
WantedBy=multi-user.target
```

```bash
sudo systemctl enable bot-vuelos
sudo systemctl start bot-vuelos
```

---

## Sobre los límites de la API

| API | Requests gratis/mes | Notas |
|---|---|---|
| **Airlabs** *(usada aquí)* | **1 000** | Ideal para grupos pequeños |
| AviationStack | 100 | Demasiado restrictivo |
| AeroDataBox (RapidAPI) | ~500 | Alternativa con datos muy detallados |
| OpenSky Network | Sin límite | Solo posición GPS, sin estado/horarios |

Con 1 000 requests al mes y vuelos de 4 h de ventana (16 requests por vuelo),
puedes monitorizar hasta **~60 vuelos/mes** de forma gratuita.

---

## Estructura del proyecto

```
boy.py          ← Bot completo (lógica, API, BD, comandos, scheduler)
requirements.txt
.env.example    ← Plantilla de configuración
.env            ← Tu configuración real (¡no subir a git!)
flights.db      ← Base de datos SQLite (se crea automáticamente)
```

---

## Notas técnicas

- La base de datos es un archivo SQLite local (`flights.db`). No necesitas
  instalar ningún servidor de BD.
- El scheduler usa el **JobQueue** de `python-telegram-bot`, que corre en el
  mismo bucle de eventos que el bot.
- Los vuelos sin hora de salida conocida se reintentan **cada hora** para no
  gastas requests hasta que la API devuelva datos.
- Un vuelo se desactiva automáticamente cuando su estado es `landed` o
  `cancelled`, o cuando la ventana de monitorización termina.
