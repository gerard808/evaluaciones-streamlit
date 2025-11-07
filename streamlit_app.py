# streamlit_app.py
# Web GUI para generar PDFs de Evaluación Estudiantil desde CSV + Excel
# Ejecutar local:  streamlit run streamlit_app.py
# Desplegar en la nube: Streamlit Cloud / Hugging Face Spaces / Render

import io
import os
import re
import shutil
import tempfile
import zipfile
from typing import List, Tuple

import matplotlib
matplotlib.use("Agg")  # render sin display
import matplotlib.pyplot as plt
import pandas as pd
import streamlit as st
from reportlab.lib.pagesizes import letter
from reportlab.pdfgen import canvas
from reportlab.lib.utils import ImageReader

# ==========================
# Utilidades y catálogos
# ==========================

def sanitize_filename(s: str) -> str:
    return (
        s.replace("/", "-")
         .replace("\\", "-")
         .replace(":", "-")
         .replace("|", "-")
         .replace("*", "-")
         .replace("?", "-")
         .replace('"', "'")
         .strip()
    )


def clean_columns(columns):
    return [col.split(".")[0].strip() for col in columns]


def wrap_label(label: str, width: int = 23) -> str:
    import textwrap
    return textwrap.fill(label, width=width)

OPC_SATISFACCION = [
    'No satisfactorio', 'Poco satisfactorio', 'Satisfactorio',
    'Muy satisfactorio', 'Absolutamente satisfactorio'
]
OPC_PORCENTAJE = ['0 - 10 %', '11 - 25%', '41 - 60 %', '61 - 80 %', 'Más de 80%']
OPC_SI_NO = ['SI', 'NO']
OPC_METOD = [
    'Clase magistral', 'Clase expositiva con participación de estudiantes',
    'Aula invertida', 'Exposiciones por parte de los estudiantes',
    'Elaboración de trabajos en equipo durante la clase',
    'Elaboración de trabajos en equipo fuera de la instancia de clase'
]
OPC_ELEM = ['Plataforma EVA-Fder', 'Presentaciones', 'Audios', 'Videos', 'Materiales impresos', 'Pizarrón']
OPC_PRUEBAS = [
    'Pruebas objetivas (múltiple opción / verdadero-falso)',
    'Preguntas abiertas (o de desarrollo)', 'Análisis de casos', 'Análisis de jurisprudencia',
    'Producciones escritas (externos)', 'Otros',
    'Dinámicas de grupo durante la clase (juego de roles, debates, etc.)',
    'Presentaciones de los estudiantes'
]
OPC_SATIS2 = ['Excelente', 'Muy bueno', 'Bueno', 'Regular', 'Malo']

# ==========================
# Gráficos y PDF
# ==========================

def create_plot(data: pd.DataFrame, column: str, teacher_name: str, out_dir: str) -> str | None:
    serie = data[column].fillna("").astype(str)
    total_resp = len(serie)
    texto = " ".join(serie.tolist())
    es_multiple = False

    if any(opt in texto for opt in OPC_SATISFACCION):
        option_list = OPC_SATISFACCION
    elif any(opt in texto for opt in OPC_PORCENTAJE):
        option_list = OPC_PORCENTAJE
    elif any(opt in texto for opt in OPC_SI_NO):
        option_list = OPC_SI_NO
    elif any(opt in texto for opt in OPC_METOD):
        option_list = OPC_METOD; es_multiple = True
    elif (any(opt in texto for opt in OPC_ELEM if opt != 'Presentaciones') or re.search(r"\bPresentaciones\b(?! de los estudiantes)", texto)):
        option_list = OPC_ELEM; es_multiple = True
    elif any(opt in texto for opt in OPC_PRUEBAS):
        option_list = OPC_PRUEBAS; es_multiple = True
    elif any(opt in texto for opt in OPC_SATIS2):
        option_list = OPC_SATIS2
    else:
        option_list = None

    final_counts = {}
    if option_list is not None:
        for opt in option_list:
            pat = re.compile(re.escape(opt))
            final_counts[opt] = serie.apply(lambda x: bool(pat.search(x))).sum()
        marcados = serie.apply(lambda x: any(re.search(rf"\b{re.escape(opt)}\b", x, re.IGNORECASE) for opt in option_list)).sum()
        final_counts["Sin respuesta"] = total_resp - marcados
    else:
        tokens = serie.str.split(r"\s+", regex=True).explode()
        counts = tokens.value_counts().sort_index().to_dict()
        final_counts = counts
        final_counts["Sin respuesta"] = (serie == "").sum()

    final_counts_filtered = {k: int(v) for k, v in final_counts.items() if k != "Sin respuesta"}
    total = sum(final_counts_filtered.values())
    if total <= 0:
        return None

    labels = list(final_counts_filtered.keys())
    values = list(final_counts_filtered.values())

    plt.figure(figsize=(6, 6))
    wedges, texts, autotexts = plt.pie(values, autopct=lambda pct: f"{pct:.1f}%" if pct > 0 else '')
    raw_legends = [f"{lbl}: {final_counts_filtered[lbl]}" for lbl in labels]
    legend_labels = [wrap_label(lbl, width=23) for lbl in raw_legends]
    plt.legend(
        wedges, legend_labels,
        title="Opciones múltiples" if es_multiple else "Opciones",
        loc="center left", bbox_to_anchor=(1, 0, 0.5, 1), fontsize=10,
        labelspacing=0.8, handletextpad=0.5, columnspacing=1.0,
    )
    plt.tight_layout()
    filename = os.path.join(out_dir, f"temp_{teacher_name}_{sanitize_filename(column)}.png").replace(" ", "_")
    plt.savefig(filename, dpi=300)
    plt.close()
    return filename


def wrap_text_rl(c: canvas.Canvas, text: str, max_width: float, font_name: str, font_size: int):
    words = text.split()
    lines, current = [], ""
    for w in words:
        t = w if not current else current + " " + w
        if c.stringWidth(t, font_name, font_size) <= max_width:
            current = t
        else:
            if current:
                lines.append(current)
            current = w
    if current:
        lines.append(current)
    return lines


def draw_page_number(c: canvas.Canvas, width: float):
    c.setFont("Helvetica", 12)
    c.drawRightString(width - 40, 15, f"{c.getPageNumber()}")


def create_pdf_for_survey(
    df: pd.DataFrame, csv_filename: str,
    unidad_curricular: str, carrera_extra: str,
    dias: str, horario: str, docente_encargado: str,
    comienza: str, finaliza: str, modalidad: str, matr: str,
    colab_1: str, colab_2: str, colab_3: str,
    header_img_bytes: bytes | None,
    output_dir: str,
) -> str:
    parts = os.path.basename(csv_filename).split('_')
    year = parts[0] if len(parts) > 0 else "AñoDesconocido"
    semester = parts[1] if len(parts) > 1 else "SemestreDesconocido"
    codigo_mat = parts[2] if len(parts) > 2 else "CodigoMateriaDesconocida"
    ch_code = parts[3] if len(parts) > 3 else "Curso"

    raw_pdf = f"{codigo_mat}_{ch_code}_{unidad_curricular}_{carrera_extra}.pdf"
    pdf_path = os.path.join(output_dir, sanitize_filename(raw_pdf))

    c = canvas.Canvas(pdf_path, pagesize=letter)
    width, height = letter

    # Encabezado
    y_after_img = height - 70
    if header_img_bytes:
        try:
            header_img = ImageReader(io.BytesIO(header_img_bytes))
            iw, ih = header_img.getSize()
            scale = width / iw
            dw, dh = iw * scale, ih * scale
            c.drawImage(header_img, 0, height - dh, width=width, height=dh)
            y_after_img = height - dh - 20
        except Exception:
            y_after_img = height - 70

    c.setFont("Helvetica-Bold", 16)
    c.drawCentredString(width / 2, y_after_img, "Evaluación estudiantil de curso")
    c.setFont("Helvetica", 14)
    x_margin = 50
    c.drawString(x_margin, y_after_img - 40, f"Año: {year}, Semestre: {semester}")
    c.drawString(x_margin, y_after_img - 60, f"Docente encargado: {docente_encargado}")
    c.drawString(x_margin, y_after_img - 80, f"Docente/s colaborador/es: {colab_1} / {colab_2} / {colab_3}")
    c.drawString(x_margin, y_after_img - 100, f"Unidad Curricular: {unidad_curricular}")
    c.drawString(x_margin, y_after_img - 120, f"Carrera/s: {carrera_extra}")
    c.drawString(x_margin, y_after_img - 140, f"Código horario: {ch_code}")
    c.drawString(x_margin, y_after_img - 160, f"Días: {dias}")
    c.drawString(x_margin, y_after_img - 180, f"Horario: {horario}")
    c.drawString(x_margin, y_after_img - 200, f"Comienza: {comienza}   Finaliza: {finaliza}")
    c.drawString(x_margin, y_after_img - 220, f"Modalidad: {modalidad}")
    c.drawString(x_margin, y_after_img - 240, f"Inscriptos habilitados para hacer la encuesta: {matr}")
    c.drawString(x_margin, y_after_img - 260, f"Total respuestas: {len(df)}")
    c.line(x_margin, y_after_img - 270, width - x_margin, y_after_img - 270)
    draw_page_number(c, width)
    c.showPage()

    # Parámetros
    top_margin, bottom_margin = 30, 30
    current_y = height - top_margin
    max_width_txt = width - 100
    header_font, header_size, leading = "Helvetica", 14, 16
    spacing_hdr, spacing_img = 5, 10
    max_img_w, max_img_h = 500, 300

    cols_all = df.columns.tolist()
    cols_to_plot = cols_all[8:-1] if len(cols_all) >= 10 else []
    open_field = cols_all[-1] if cols_all else None

    tmp_img_dir = tempfile.mkdtemp(prefix="charts_")

    for col in cols_to_plot:
        img_file = create_plot(df, col, codigo_mat, tmp_img_dir)
        if img_file is None:
            continue

        lines = wrap_text_rl(c, col, max_width_txt, header_font, header_size)
        hdr_h = len(lines) * leading

        img = ImageReader(img_file)
        iw, ih = img.getSize()
        scale = min(max_img_w / iw, max_img_h / ih, 1.0)
        dw, dh = iw * scale, ih * scale
        block_h = hdr_h + spacing_hdr + dh + spacing_img

        if current_y - block_h < bottom_margin:
            draw_page_number(c, width)
            c.showPage()
            current_y = height - top_margin

        c.setFont(header_font, header_size)
        for i, line in enumerate(lines):
            c.drawCentredString(width / 2, current_y - i * leading, line)
        current_y -= (hdr_h + spacing_hdr)

        x_img = (width - dw) / 2
        y_img = current_y - dh
        c.drawImage(img_file, x_img, y_img, width=dw, height=dh)
        current_y = y_img - spacing_img

    # Campo abierto
    if open_field is not None:
        respuestas = df[open_field].dropna().astype(str).tolist()
    else:
        respuestas = []

    if cols_to_plot:
        draw_page_number(c, width)

    if respuestas:
        c.showPage()
        c.setFont("Helvetica-Bold", 14)
        title_lines = wrap_text_rl(c, open_field, max_width_txt, "Helvetica-Bold", 14)
        y = letter[1] - 70
        for line in title_lines:
            if y < bottom_margin:
                draw_page_number(c, width)
                c.showPage()
                y = letter[1] - top_margin
            c.drawCentredString(width/2, y, line)
            y -= leading
        y -= leading

        for idx, resp in enumerate(respuestas, start=1):
            header = f"Comentario {idx}:"
            c.setFont("Helvetica-Bold", 12)
            header_lines = wrap_text_rl(c, header, max_width_txt, "Helvetica-Bold", 12)
            for hl in header_lines:
                if y < bottom_margin:
                    draw_page_number(c, width)
                    c.showPage()
                    y = letter[1] - top_margin
                c.drawString(50, y, hl)
                y -= leading

            c.setFont("Helvetica", 12)
            resp_lines = wrap_text_rl(c, resp, max_width_txt, "Helvetica", 12)
            for ln in resp_lines:
                if y < bottom_margin:
                    draw_page_number(c, width)
                    c.showPage()
                    y = letter[1] - top_margin
                c.drawString(60, y, ln)
                y -= leading
            y -= leading

        draw_page_number(c, width)
        c.showPage()

    c.save()

    # Limpieza de imágenes temporales
    shutil.rmtree(tmp_img_dir, ignore_errors=True)
    return pdf_path


# ==========================
# Lógica de la app Streamlit
# ==========================

st.set_page_config(page_title="Evaluaciones · Generación de PDFs", layout="wide")
st.title("Evaluaciones estudiantiles → PDFs")

st.markdown(
    "Suba el Excel maestro y uno o más CSV. Opcionalmente, suba un banner para el encabezado. Se generará un ZIP con todos los PDFs."
)

with st.sidebar:
    st.header("Parámetros")
    header_img = st.file_uploader("Imagen de encabezado (opcional)", type=["png", "jpg", "jpeg", "bmp"])  # noqa: E231

excel_file = st.file_uploader("Excel maestro (.xlsx)", type=["xlsx"])  # noqa: E231
csv_files = st.file_uploader("CSVs de respuestas", type=["csv"], accept_multiple_files=True)

run = st.button("Generar PDFs")

if run:
    if not excel_file or not csv_files:
        st.error("Debe subir el Excel maestro y al menos un CSV.")
    else:
        with st.spinner("Procesando…"):
            tmpdir = tempfile.mkdtemp(prefix="evalpdf_")
            outdir = os.path.join(tmpdir, "salida")
            os.makedirs(outdir, exist_ok=True)

            # Guardar Excel a disco y leerlo
            excel_path = os.path.join(tmpdir, sanitize_filename(excel_file.name))
            with open(excel_path, "wb") as f:
                f.write(excel_file.getbuffer())

            hojas = pd.read_excel(excel_path, sheet_name=None)
            df_curso = pd.concat(hojas.values(), ignore_index=True)
            df_curso["ID - EVA"] = df_curso["ID - EVA"].astype(str).str.strip()
            df_curso["CH"] = df_curso["CH"].fillna("").astype(str).str.replace(r"\.0$", "", regex=True).str.strip()

            # Validación mínima
            required_cols = [
                "ID - EVA", "CH", "Unidad curricular", "Carrera", "Días", "Horario",
                "Docente encargado", "Comienza", "Finaliza", "Modalidad", "No. matr.",
                "Colaborador 1", "Colaborador 2", "Colaborador 3",
            ]
            missing = [c for c in required_cols if c not in df_curso.columns]
            if missing:
                st.error(f"Faltan columnas en Excel: {missing}")
            else:
                generated: List[str] = []
                failed: List[str] = []
                header_bytes = header_img.getvalue() if header_img else None

                for upl in csv_files:
                    csv_path = os.path.join(tmpdir, sanitize_filename(upl.name))
                    with open(csv_path, "wb") as f:
                        f.write(upl.getbuffer())

                    # extraer ID y CH
                    id_match = re.search(r"ID(\d+)", upl.name)
                    ch_match = re.search(r"CH(\d+)", upl.name)
                    id_eva = id_match.group(1) if id_match else None
                    ch_val = ch_match.group(1) if ch_match else None

                    df_match = df_curso[(df_curso["ID - EVA"] == id_eva) & (df_curso["CH"] == ch_val)]
                    if df_match.empty:
                        failed.append(upl.name + " · sin coincidencia en Excel")
                        continue

                    try:
                        df = pd.read_csv(csv_path)
                        df.columns = clean_columns(df.columns)
                        if len(df.columns) >= 8:
                            df = df.dropna(how='all', subset=df.columns[7:])
                        if df.empty:
                            failed.append(upl.name + " · CSV sin datos útiles")
                            continue

                        meta = df_match.iloc[0]
                        pdf_path = create_pdf_for_survey(
                            df, upl.name,
                            meta.get("Unidad curricular", "Desconocida"),
                            meta.get("Carrera", "Desconocida"),
                            meta.get("Días", "No especificado"),
                            meta.get("Horario", "No especificado"),
                            meta.get("Docente encargado", "No especificado"),
                            meta.get("Comienza", "No especificado"),
                            meta.get("Finaliza", "No especificado"),
                            meta.get("Modalidad", "No especificado"),
                            str(meta.get("No. matr.", "No especificado")),
                            str(meta.get("Colaborador 1", "")),
                            str(meta.get("Colaborador 2", "")),
                            str(meta.get("Colaborador 3", "")),
                            header_bytes,
                            output_dir=outdir,
                        )
                        generated.append(pdf_path)
                    except Exception as e:
                        failed.append(upl.name + f" · error: {e}")

                # Armar ZIP de salida
                if generated:
                    zip_path = os.path.join(tmpdir, "reportes_pdfs.zip")
                    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
                        for p in generated:
                            zf.write(p, arcname=os.path.basename(p))
                    with open(zip_path, "rb") as f:
                        st.download_button(
                            label="Descargar ZIP de PDFs",
                            data=f.read(),
                            file_name="reportes_pdfs.zip",
                            mime="application/zip",
                        )

                # Mostrar resumen
                st.subheader("Resumen")
                st.write({"generados": len(generated), "fallidos": len(failed)})
                if failed:
                    st.write("Problemas detectados:")
                    for item in failed:
                        st.write("- ", item)

            # Limpieza del tmp cuando termine la sesión es manejada por el sistema
