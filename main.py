
import streamlit as st
import pdfplumber
import pandas as pd
import re
import io
import zipfile
from datetime import datetime

st.set_page_config(
    page_title="Docket Data Extractor",
    page_icon="📄",
    layout="centered"
)

st.title("📄 Docket Data Extractor")

st.markdown("""
Upload one or more docket PDFs.

The application will:

- Extract labour information
- Extract material tonnages
- Sort PDFs into folders by Material Type and Date
- Generate tracker spreadsheets
- Package everything into a single downloadable ZIP file

When ready, click **Extract Data**.
""")


# =====================================================
# HELPERS
# =====================================================

def format_folder_date(date_str):
    try:
        return datetime.strptime(
            date_str,
            "%d/%m/%Y"
        ).strftime("%d.%m.%Y")
    except:
        return "Unknown Date"


def convert_duration(text):

    if not text:
        return ""

    text = text.lower().strip()

    mins_match = re.search(r"(\d+)\s*minute", text)
    if mins_match:
        return round(int(mins_match.group(1)) / 60, 2)

    hrs_match = re.search(r"(\d+)\s*hour", text)
    if hrs_match:
        return float(hrs_match.group(1))

    return text


def normalise_material(material):

    material_lower = material.lower()

    if "heid" in material_lower and "sand" in material_lower:
        return "Heidelberg Sand"

    elif "roadbase" in material_lower:
        return "Crushed Rock Basecourse"

    return material.strip()


def extract_material(page2):

    material_match = re.search(
        r"\)\s*-\s*([A-Za-z0-9\s]+?)\s+\d+\.\d+\s*T",
        page2,
        re.DOTALL
    )

    if material_match:
        return material_match.group(1).strip()

    return ""


def extract_times(page1, page2):

    combined_text = page1 + "\n" + page2

    start_time = ""
    end_time = ""
    break_time = ""
    travel_time = ""

    time_match = re.search(
        r"(\d{2}:\d{2}\s+[AP]M)\s+"
        r"(\d{2}:\d{2}\s+[AP]M)\s+"
        r"(\d+\s+(?:Minutes?|Hour|Hours?))\s+"
        r"(\d+\s+(?:Minutes?|Hour|Hours?))",
        combined_text,
        re.IGNORECASE
    )

    if time_match:

        start_time = time_match.group(1).strip()
        end_time = time_match.group(2).strip()

        break_time = convert_duration(
            time_match.group(3)
        )

        travel_time = convert_duration(
            time_match.group(4)
        )

    return (
        start_time,
        end_time,
        break_time,
        travel_time
    )


def extract_total_tonnage(page2):

    total_match = re.search(
        r"\)\s*-\s*[^\n]+\s+(\d+\.\d+)\s*T\s+New Activity",
        page2,
        re.DOTALL
    )

    if total_match:
        return float(total_match.group(1))

    return ""


def extract_docket(uploaded_file):

    try:

        with pdfplumber.open(uploaded_file) as pdf:

            page1 = pdf.pages[0].extract_text() or ""

            page2 = ""
            if len(pdf.pages) > 1:
                page2 = pdf.pages[1].extract_text() or ""

        # -------------------------
        # Docket
        # -------------------------

        docket_match = re.search(
            r"Docket\s+(.+)",
            page1
        )

        docket = (
            docket_match.group(1).strip()
            if docket_match
            else ""
        )

        # -------------------------
        # Date
        # -------------------------

        date_match = re.search(
            r"(\d{2}/\d{2}/\d{4})",
            page1
        )

        date = (
            date_match.group(1)
            if date_match
            else ""
        )

        # -------------------------
        # Times
        # -------------------------

        (
            start_time,
            end_time,
            break_time,
            travel_time
        ) = extract_times(page1, page2)

        # -------------------------
        # Material
        # -------------------------

        material_raw = extract_material(page2)

        material = normalise_material(material_raw)

        # -------------------------
        # Total tonnage
        # -------------------------

        total_tonnage = extract_total_tonnage(page2)

        # -------------------------
        # Individual tonnages
        # -------------------------

        tonnages = re.findall(
            r"New Activity\s+([\d.]+)\s*T",
            page2
        )

        tonnages = [float(x) for x in tonnages]

        return {
            "success": True,
            "pdf_name": uploaded_file.name,
            "date": date,
            "folder_date": format_folder_date(date),
            "docket": docket,
            "material": material,
            "start_time": start_time,
            "end_time": end_time,
            "break_time": break_time,
            "travel_time": travel_time,
            "tonnages": tonnages,
            "total_tonnage": total_tonnage,
            "file_bytes": uploaded_file.getvalue()
        }

    except Exception as e:

        return {
            "success": False,
            "pdf_name": uploaded_file.name,
            "error": str(e)
        }


# =====================================================
# UI
# =====================================================

uploaded_files = st.file_uploader(
    "Upload Docket PDFs",
    type=["pdf"],
    accept_multiple_files=True
)

# =====================================================
# PROCESS
# =====================================================

if uploaded_files:

    st.info(f"{len(uploaded_files)} PDF(s) loaded.")

    if st.button("Extract Data"):

        labour_rows = []
        sand_rows = []
        roadbase_rows = []
        summary_rows = []
        error_rows = []

        extracted = []

        progress = st.progress(0)

        for i, uploaded_file in enumerate(uploaded_files):

            result = extract_docket(uploaded_file)

            if result["success"]:

                extracted.append(result)

                labour_rows.append({
                    "Date": result["date"],
                    "Docket Number": result["docket"],
                    "Start Time": result["start_time"],
                    "End Time": result["end_time"],
                    "Break Time": result["break_time"],
                    "Travel Time": result["travel_time"]
                })

                summary_rows.append({
                    "Date": result["date"],
                    "Docket": result["docket"],
                    "Material": result["material"],
                    "Total Tonnage": result["total_tonnage"]
                })

                for tonnage in result["tonnages"]:

                    row = {
                        "Date": result["date"],
                        "Zone": "",
                        "Dockets": result["docket"],
                        "Tonnage": tonnage
                    }

                    if result["material"] == "Heidelberg Sand":
                        sand_rows.append(row)

                    elif result["material"] == "Crushed Rock Basecourse":
                        roadbase_rows.append(row)

            else:

                error_rows.append({
                    "PDF": result["pdf_name"],
                    "Error": result["error"]
                })

            progress.progress((i + 1) / len(uploaded_files))

        # =================================================
        # EXCEL
        # =================================================

        excel_buffer = io.BytesIO()

        with pd.ExcelWriter(
            excel_buffer,
            engine="openpyxl"
        ) as writer:

            pd.DataFrame(labour_rows).to_excel(
                writer,
                sheet_name="Labour Tracker",
                index=False
            )

            pd.DataFrame(sand_rows).to_excel(
                writer,
                sheet_name="Heidelberg Sand Tracker",
                index=False
            )

            pd.DataFrame(roadbase_rows).to_excel(
                writer,
                sheet_name="Crushed Rock Basecourse Tracker",
                index=False
            )

            pd.DataFrame(summary_rows).to_excel(
                writer,
                sheet_name="Docket Summary",
                index=False
            )

            pd.DataFrame(error_rows).to_excel(
                writer,
                sheet_name="Extraction Errors",
                index=False
            )

        excel_buffer.seek(0)

        # =================================================
        # ZIP OUTPUT
        # =================================================

        zip_buffer = io.BytesIO()

        with zipfile.ZipFile(
            zip_buffer,
            "w",
            zipfile.ZIP_DEFLATED
        ) as zf:

            zf.writestr(
                "Docket_Tracker.xlsx",
                excel_buffer.getvalue()
            )

            for item in extracted:

                folder_path = (
                    f"Sorted PDFs/"
                    f"{item['material']}/"
                    f"{item['folder_date']}/"
                    f"{item['pdf_name']}"
                )

                zf.writestr(
                    folder_path,
                    item["file_bytes"]
                )

        zip_buffer.seek(0)

        st.success("Extraction Complete")

        st.download_button(
            label="📥 Download Processed ZIP",
            data=zip_buffer.getvalue(),
            file_name="Processed_Dockets.zip",
            mime="application/zip"
        )

