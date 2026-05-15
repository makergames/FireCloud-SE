<p align="center">
  <img src="https://forthcoming-apricot-rrurqmtnnn.edgeone.app/FireCloud_logo-removebg-preview.png" alt="FireCloud SE" width="200"/>
</p>

<h1 align="center">🔥 FireCloud-SE</h1>
<h3 align="center"><em>Descubre una nueva cloud libre</em></h3>

<p align="center">
  <img src="https://img.shields.io/badge/Versión-0.1-orange" alt="Versión"/>
  <img src="https://img.shields.io/badge/License-MIT-yellow" alt="License"/>
  <img src="https://img.shields.io/badge/Estado-En%20desarrollo-brightgreen" alt="Estado"/>
  <img src="https://img.shields.io/badge/Hecho%20en-España-ff0000" alt="España"/>
</p>

---

## 📖 Introducción

FireCloud SE (System Edition). Probablemente el primer OS visual nativo en navegador.

Después de bastante tiempo de desarrollo y bastantes dolores de cabeza, no me detuve. Preferí seguir adelante a pesar de todas las dificultades técnicas. Miles de pruebas, tests y bastantes problemas después, conseguí crear una versión más o menos estable. De momento no digamos que tenga una gran velocidad ni mucha potencia, pero es útil para el día a día.

---

## 🕐 Historia del proyecto

La verdad es que no fue un proyecto nada fácil. Lleva **3 años en desarrollo** y tuvo varias etapas previas.

| Etapa | Nombre | Descripción |
|-------|--------|-------------|
| 🦴 Esqueleto | **Maker OS** | Hecho en Genially. Versión esquelética para hacerme una idea de cómo quedaría el proyecto final. |
| 🧪 Prototipos | **HTML + JS + CSS + WebAssembly** | Cientos de versiones previas. Aunque no terminaron de gustarme, |
| 🔥 Actual | **FireCloud SE 0.1** | Versión más o menos estable, útil para el día a día. |

---


# FireCloud SE — Explicación detallada de funcionamiento

FireCloud SE es un **emulador en la nube** que ejecuta un entorno Linux completo dentro de su navegador web. No es un simple escritorio remoto, sino una emulación completa de un sistema gráfico Linux, con capacidad de ejecutar aplicaciones nativas, Windows (via Wine) y Android (via Waydroid), todo accesible desde cualquier navegador moderno.

Este documento explica **cómo funciona internamente** FireCloud SE, desde el servidor X virtual hasta la interfaz web.

## Visión general

FireCloud SE consta de dos partes principales:

1. **Backend en Python (Flask)** que orquesta un escritorio X11 virtual, captura la pantalla en tiempo real, gestiona el audio, expone una API REST para control remoto y administra las aplicaciones emuladas.
2. **Frontend en HTML/CSS/JS** que muestra la interfaz del sistema emulado en el navegador, envía eventos de ratón/teclado, reproduce el audio y proporciona una experiencia de escritorio completa.

Todo el sistema se ejecuta en un servidor sin necesidad de monitor físico ni tarjeta gráfica: utiliza un servidor X virtual (Xvfb) y un window manager ligero (Openbox).

## Componentes clave y su interacción

### 1. Servidor X virtual (Xvfb)
- Al iniciar el backend, se lanza `Xvfb :1 -screen 0 1280x720x24`.
- Crea un entorno gráfico completamente en memoria, sin salida por pantalla física.
- Todas las aplicaciones emuladas se ejecutan usando `DISPLAY=:1`.

### 2. Window manager (Openbox)
- Se ejecuta dentro del mismo `DISPLAY`.
- Gestiona la posición, tamaño y decoración de las ventanas de las aplicaciones emuladas.
- Permite que se comporten como en un escritorio normal.

### 3. Captura de pantalla (bucle de emulación de vídeo)
- Un hilo independiente captura el framebuffer de Xvfb usando `mss` (captura rápida).
- La captura se realiza a una frecuencia configurable (por defecto 24 FPS).
- Cada frame se comprime a JPEG con calidad ajustable.
- El último frame se guarda en una variable global con sincronización (`Condition`).

### 4. Streaming de vídeo (MJPEG)
- El endpoint `/stream` devuelve un flujo `multipart/x-mixed-replace`.
- El navegador mantiene una conexión persistente y recibe continuamente nuevos fotogramas.
- El cliente muestra el stream en un elemento `<img>`, actualizándose automáticamente.

### 5. Streaming de audio
- El backend configura PulseAudio con un `null-sink` llamado `cloudsink`.
- Todas las aplicaciones emuladas envían su salida de audio a ese sink.
- Un proceso `ffmpeg` captura el monitor del sink y lo convierte a PCM s16le (raw).
- El endpoint `/audio` envía ese flujo PCM al navegador.
- El frontend usa `WebAudio` (con `ScriptProcessorNode` o similar) para recibir los datos y reproducirlos.
- La reproducción se activa tras el primer clic del usuario (requisito de autoplay de los navegadores).

### 6. Control remoto (emulación de entrada)
- El frontend captura eventos de ratón y teclado sobre la imagen del escritorio emulado.
- Calcula coordenadas virtuales en función del tamaño real del entorno emulado y del contenedor.
- Envía peticiones POST a la API:
  - `/api/click`, `/api/mousemove`, `/api/scroll`
  - `/api/key`, `/api/type`
- El backend ejecuta comandos `xdotool` sobre el `DISPLAY=:1` para inyectar los eventos en el servidor X virtual.
- `xdotool` permite mover el ratón, hacer clic, pulsar teclas, etc., exactamente igual que si el usuario estuviera interactuando directamente con la máquina emulada.

### 7. Gestión de aplicaciones emuladas
- El backend lee archivos `.desktop` de `/usr/share/applications` y `~/.local/share/applications`.
- Usa `configparser` para extraer nombre, comando, icono, etc.
- Filtra aplicaciones con `NoDisplay=true` o `Hidden=true`.
- Expone la lista a través de `/api/apps` (endpoint GET).
- El frontend muestra las apps en un panel con búsqueda, orden arrastrable y menú contextual.
- Al hacer clic en una app, el frontend llama a `/api/launch` con `{exec, name}`.
- El backend ejecuta el comando en el `DISPLAY=:1` usando `subprocess.Popen`.
- Además, puede enfocar la ventana recién abierta con `xdotool windowactivate`.

### 8. Sistema de archivos emulado
- Endpoints como `/api/files`, `/api/file/read`, `/api/file/save`, `/api/file/delete` permiten navegar y editar archivos dentro del entorno emulado.
- El backend restringe el acceso a directorios seguros (home, descargas, etc.) para evitar escapes.
- El frontend incluye un explorador de archivos con previsualización de imágenes/vídeos.
- Se puede abrir un archivo en el editor integrado (texto) o con la aplicación por defecto (`xdg-open`).

### 9. Personalización del entorno emulado
- El fondo de pantalla del escritorio emulado se puede cambiar mediante presets (colores sólidos) o subiendo una imagen.
- El backend aplica el fondo usando `xsetroot` (colores) o `feh` (imágenes).
- El estado del wallpaper se guarda en `~/.config/firecloud_se/wallpaper.json` para persistencia.
- El frontend también refleja el fondo en su propio CSS para mejorar la experiencia visual (aunque el fondo real ya se ve en el stream).

### 10. Interfaz de usuario en el navegador
- La página HTML incluye:
  - Barra de herramientas superior (botones de paneles, reloj, notificaciones).
  - Barra de tareas (muestra ventanas abiertas consultando `/api/windows`).
  - Imagen del escritorio emulado (`<img src="/stream">`).
  - Paneles flotantes (Apps, Ajustes, Archivos, Editor, Notificaciones) arrastrables y redimensionables.
  - Dock inferior con 8 accesos directos configurables (persistencia en `localStorage`).
- El frontend actualiza periódicamente la barra de tareas, la información del sistema (RAM, CPU, disco) y el estado de las aplicaciones mediante peticiones AJAX.

### 11. Sincronización de estado y notificaciones
- El backend mantiene una revisión de la lista de aplicaciones (`/api/apps-revision`).
- El frontend consulta periódicamente la revisión y recarga la lista si cambió (por ejemplo, tras instalar una nueva aplicación dentro del emulador).
- Las notificaciones del sistema se almacenan en `localStorage` del navegador y se muestran en un panel dedicado.

## Flujo de datos completo (ejemplo: emular Firefox)

1. Usuario hace clic en el icono de Firefox en el panel de Apps del frontend.
2. Frontend envía `POST /api/launch` con `{exec: "firefox", name: "Firefox"}`.
3. Backend recibe la petición, lanza `subprocess.Popen` para ejecutar Firefox en el `DISPLAY=:1`.
4. Firefox se abre dentro del servidor X virtual (Xvfb).
5. El bucle de captura detecta el cambio en el framebuffer y envía nuevos frames JPEG al stream.
6. El navegador actualiza la imagen mostrando la ventana de Firefox.
7. El frontend actualiza la barra de tareas con la nueva ventana.

## Flujo de eventos de ratón (ejemplo: clic en un botón dentro del emulador)

1. Usuario hace clic sobre la imagen del escritorio emulado en el navegador.
2. JavaScript calcula las coordenadas virtuales en el entorno emulado (teniendo en cuenta `object-fit: contain` y la relación de aspecto).
3. Envía `POST /api/click` con `{x, y, button:1}`.
4. Backend ejecuta `xdotool mousemove x y` y `xdotool click 1` sobre el `DISPLAY=:1`.
5. Xvfb recibe el evento y lo entrega a la aplicación activa (ej. Firefox).
6. La aplicación reacciona (abre un enlace, etc.).
7. El siguiente frame capturado refleja el cambio en el stream.

## Persistencia y datos guardados

| Elemento | Dónde se guarda | Propósito |
|----------|----------------|-----------|
| Orden de aplicaciones en el grid | `localStorage` del navegador | Recordar la disposición personalizada |
| Configuración del dock | `localStorage` del navegador | Accesos directos anclados |
| Notas personales | `localStorage` del navegador | Contenido del bloc de notas |
| Notificaciones | `localStorage` del navegador | Historial de notificaciones del emulador |
| Fondo de pantalla | `~/.config/firecloud_se/wallpaper.png` y `wallpaper.json` | Imagen de fondo y su estado |
| Prefijo de Wine | `~/.config/firecloud_se/wineprefix` | Entorno Wine para ejecutar aplicaciones Windows emuladas |

## Consideraciones técnicas importantes

- **Rendimiento:** La captura y compresión JPEG se realiza en un hilo separado para no bloquear la API. Calidad y FPS ajustables permiten equilibrar ancho de banda y fluidez.
- **Latencia:** El streaming MJPEG tiene latencia típica de 100-300ms. En redes locales es casi imperceptible.
- **Audio:** Se usa PCM s16le sin comprimir (~1.5 Mbps por canal estéreo a 48 kHz). Se puede comprimir con Opus modificando el backend.
- **Seguridad:** No hay autenticación por defecto; para exponer a internet se recomienda un proxy inverso con autenticación básica.
- **Compatibilidad:** Funciona en cualquier navegador moderno (Chrome, Firefox, Edge, Safari). El audio requiere soporte de WebAudio.

## Limitaciones conocidas

- Algunas aplicaciones 3D (OpenGL acelerado) pueden no funcionar correctamente en Xvfb.
- El streaming de audio necesita que el usuario haga clic en la página para comenzar (política de autoplay de los navegadores).
- Waydroid puede requerir configuración adicional del sistema (sesión wayland, etc.) y no funcionar en todos los entornos.
- El entorno emulado es de un solo monitor; siempre es una única pantalla virtual.

---

Esta explicación cubre el **qué**, **cómo** y **por qué** de cada componente de FireCloud SE. Para instrucciones de instalación, uso o desarrollo, consulta otros documentos del proyecto.


# 💡 Recomendaciones de ejecución para FireCloud SE

FireCloud SE puede ejecutarse en diferentes entornos. A continuación se explican las opciones recomendadas según tus necesidades.

## ☁️ Ejecutar en la nube (recomendado)

La forma más sencilla de ejecutar FireCloud SE es usar un entorno de desarrollo en la nube. **GitHub Codespaces** es la opción recomendada.

### GitHub Codespaces

- Proporciona **8 GB de RAM** y **32 GB de almacenamiento**.
- Acceso desde cualquier navegador, sin necesidad de instalar nada localmente.
- Configuración automática: abre el repositorio en Codespaces y ejecuta el script.
- La URL de acceso se genera automáticamente.

### Cómo usarlo con FireCloud SE

1. Crea un repositorio en GitHub con el código de FireCloud SE.
2. Haz clic en el botón `Code` → `Codespaces` → `Create codespace on main`.
3. En la terminal, ejecuta el script de instalación o directamente `python3 firecloud_se.py`.
4. Abre la URL que aparece (normalmente `https://<nombre>-5000.preview.app.github.dev`).
5. Disfruta de FireCloud SE en la nube.

## ☁️ Otros ejecutores de código en la nube

Cualquier servicio que proporcione una terminal Linux con acceso a puertos HTTP puede ejecutar FireCloud SE. Las características de RAM y almacenamiento varían según el proveedor y el plan elegido.

| Servicio | RAM típica | Almacenamiento típico |
|----------|------------|----------------------|
| **Gitpod** | 4-8 GB | 20-50 GB |
| **Replit** | 2-4 GB | 2-5 GB |
| **Google Cloud Shell** | 1.7 GB | 5 GB |
| **AWS Cloud9** | 1-4 GB | 5-10 GB |
| **CodeSandbox** | 2-4 GB | 2-5 GB |

Para una experiencia fluida con FireCloud SE, se recomienda al menos **4 GB de RAM** y **10 GB de almacenamiento**.

## 💻 Ejecutar en local (tu propio equipo)

Si prefieres ejecutar FireCloud SE en tu máquina local, los recursos serán los de tu propio dispositivo:

- **RAM:** La que tenga tu ordenador (recomendado 4 GB mínimo).
- **Almacenamiento:** El espacio disponible en tu disco duro.
- **CPU:** Cualquier procesador moderno funcionará correctamente.

### Ventajas de ejecutar localmente
- Sin dependencia de conexión a internet (excepto para el navegador).
- Control total sobre la configuración.

### Desventajas
- Requiere instalar todas las dependencias manualmente.
- Consume recursos de tu equipo.
- Para acceder desde fuera de tu red, necesitas configurar reenvío de puertos o usar un túnel (ngrok, etc.).

## 📊 Resumen de recursos

| Entorno | RAM recomendada | Almacenamiento recomendado | Acceso externo |
|---------|----------------|----------------------------|----------------|
| **GitHub Codespaces** | 8 GB | 32 GB | URL pública automática |
| **Otros ejecutores nube** | 4+ GB (según proveedor) | 10+ GB (según proveedor) | Variable |
| **Local** | 4 GB mínimo | 5 GB mínimo | Requiere configuración manual |

## 🚀 Conclusión

**Para la mejor experiencia sin configuraciones adicionales, usa GitHub Codespaces.** Obtienes recursos generosos (8 GB RAM, 32 GB almacenamiento) y una URL accesible desde cualquier lugar. Si prefieres ejecutarlo en tu propio equipo, puedes hacerlo con recursos ilimitados (los de tu máquina) a cambio de una instalación manual.

---

*Nota: Las características de los servicios en la nube pueden cambiar. Consulta la documentación actualizada de cada proveedor.*

## 🚀 Instalación

```bash

#Cuando lo pegues en la consola haz click en entrer aunque no salga el texto

#!/bin/bash

# Colores para output
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m' # No Color

echo -e "${GREEN}========================================${NC}"
echo -e "${GREEN}Instalando dependencias para Linux Cloud OS${NC}"
echo -e "${GREEN}========================================${NC}"

# Detectar distribución
if [ -f /etc/os-release ]; then
    . /etc/os-release
    OS=$ID
    VER=$VERSION_ID
else
    echo -e "${RED}No se pudo detectar la distribución${NC}"
    exit 1
fi

echo -e "${YELLOW}Distribución detectada: $OS $VER${NC}"

# Función para instalar en Debian/Ubuntu
install_debian() {
    echo -e "${YELLOW}Actualizando repositorios...${NC}"
    sudo apt update
    
    echo -e "${YELLOW}Instalando dependencias del sistema...${NC}"
    sudo apt install -y \
        xvfb \
        x11-utils \
        openbox \
        xdotool \
        xterm \
        feh \
        wmctrl \
        pulseaudio \
        pulseaudio-utils \
        ffmpeg \
        dbus-x11 \
        x11-xserver-utils \
        python3-pip \
        python3-dev \
        build-essential \
        libxcb-shm0 \
        libxcb-shape0 \
        libxcb-xfixes0 \
        libxcb-xinerama0 \
        libxcb-randr0 \
        libxcb-image0 \
        libxcb-keysyms1 \
        libxcb-render-util0 \
        gir1.2-gtk-3.0 \
        libnotify-bin \
        xclip \
        wget \
        curl
    
    # Instalar opcionales (no críticos)
    echo -e "${YELLOW}Instalando paquetes opcionales...${NC}"
    sudo apt install -y \
        firefox \
        chromium \
        wine64 \
        wine32 \
        winetricks || true
}

# Función para instalar en Arch Linux
install_arch() {
    echo -e "${YELLOW}Actualizando repositorios...${NC}"
    sudo pacman -Syu --noconfirm
    
    echo -e "${YELLOW}Instalando dependencias del sistema...${NC}"
    sudo pacman -S --noconfirm \
        xorg-server-xvfb \
        xorg-xprop \
        openbox \
        xdotool \
        xterm \
        feh \
        wmctrl \
        pulseaudio \
        ffmpeg \
        dbus \
        python-pip \
        python-psutil \
        base-devel \
        libxcb \
        gtk3 \
        libnotify \
        xclip
    
    # Instalar opcionales
    sudo pacman -S --noconfirm \
        firefox \
        chromium \
        wine \
        winetricks || true
}

# Función para instalar en Fedora/RHEL
install_fedora() {
    echo -e "${YELLOW}Actualizando repositorios...${NC}"
    sudo dnf check-update || true
    
    echo -e "${YELLOW}Instalando dependencias del sistema...${NC}"
    sudo dnf install -y \
        xorg-x11-server-Xvfb \
        xorg-x11-utils \
        openbox \
        xdotool \
        xterm \
        feh \
        wmctrl \
        pulseaudio \
        pulseaudio-utils \
        ffmpeg \
        dbus-x11 \
        xorg-x11-xinit \
        python3-pip \
        python3-devel \
        gcc \
        gcc-c++ \
        make \
        libxcb \
        gtk3 \
        libnotify \
        xclip
    
    # Instalar opcionales
    sudo dnf install -y \
        firefox \
        chromium \
        wine \
        winetricks || true
}

# Instalar según la distribución
case "$OS" in
    ubuntu|debian)
        install_debian
        ;;
    arch|manjaro)
        install_arch
        ;;
    fedora|rhel|centos)
        install_fedora
        ;;
    *)
        echo -e "${RED}Distribución no soportada automáticamente: $OS${NC}"
        echo -e "${YELLOW}Intentando instalar solo dependencias de pip...${NC}"
        ;;
esac

# Instalar dependencias de Python pip (siempre se ejecuta)
echo -e "${GREEN}========================================${NC}"
echo -e "${YELLOW}Instalando dependencias de Python (pip)...${NC}"
echo -e "${GREEN}========================================${NC}"

# Actualizar pip
python3 -m pip install --upgrade pip

# Instalar paquetes necesarios
pip3 install flask mss pillow

# Intentar instalar psutil (opcional)
echo -e "${YELLOW}Instalando psutil (opcional, para monitoreo)...${NC}"
pip3 install psutil || echo -e "${YELLOW}psutil no se pudo instalar, continuando...${NC}"

# Verificar instalaciones de pip
echo -e "${GREEN}========================================${NC}"
echo -e "${YELLOW}Verificando instalaciones de pip...${NC}"
echo -e "${GREEN}========================================${NC}"

pip3 list | grep -E "flask|mss|pillow|psutil" || echo -e "${RED}Algunos paquetes no se encontraron${NC}"

# Configuración post-instalación
echo -e "${GREEN}========================================${NC}"
echo -e "${YELLOW}Configuración del entorno...${NC}"
echo -e "${GREEN}========================================${NC}"

# Crear directorios necesarios
mkdir -p ~/.config/linuxcloud_os
mkdir -p ~/.local/share/applications
mkdir -p ~/Downloads
mkdir -p /tmp/.X11-unix

# Configurar permisos
sudo chmod 1777 /tmp/.X11-unix 2>/dev/null || true
mkdir -p /tmp/runtime-codespace
chmod 700 /tmp/runtime-codespace 2>/dev/null || true

# Iniciar PulseAudio si no está corriendo
if ! pulseaudio --check; then
    echo -e "${YELLOW}Iniciando PulseAudio...${NC}"
    pulseaudio --start --exit-idle-time=-1 2>/dev/null || true
fi

# Verificar comandos importantes
echo -e "${GREEN}========================================${NC}"
echo -e "${YELLOW}Verificando comandos instalados...${NC}"
echo -e "${GREEN}========================================${NC}"

COMMANDS=("Xvfb" "openbox" "xdotool" "xterm" "feh" "wmctrl" "ffmpeg" "pulseaudio" "python3")
MISSING=()

for cmd in "${COMMANDS[@]}"; do
    if command -v $cmd >/dev/null 2>&1; then
        echo -e "${GREEN}✓ $cmd${NC}"
    else
        echo -e "${RED}✗ $cmd (NO INSTALADO)${NC}"
        MISSING+=($cmd)
    fi
done

# Mostrar resumen
echo -e "${GREEN}========================================${NC}"
if [ ${#MISSING[@]} -eq 0 ]; then
    echo -e "${GREEN}✅ TODAS las dependencias están instaladas correctamente${NC}"
else
    echo -e "${YELLOW}⚠️  Faltan algunos comandos:${NC}"
    for cmd in "${MISSING[@]}"; do
        echo -e "   - $cmd"
    done
    echo -e "${YELLOW}Puedes intentar instalarlos manualmente${NC}"
fi

echo -e "${GREEN}========================================${NC}"
echo -e "${GREEN}¡Instalación completada!${NC}"
echo -e "${GREEN}========================================${NC}"
echo -e "${YELLOW}Para ejecutar tu aplicación:${NC}"
echo -e "  python3 tu_script.py"
echo -e ""
echo -e "${YELLOW}O con variables de entorno personalizadas:${NC}"
echo -e "  EMULATOR_WIDTH=1920 EMULATOR_HEIGHT=1080 EMULATOR_FPS=30 python3 tu_script.py"
echo -e "${GREEN}========================================${NC}"
