import streamlit as st
import pdfplumber
import pandas as pd
import re
import io
import zipfile
from datetime import datetime


# =====================================================
# PAGE CONFIG
# =====================================================

st.set_page_config(
    page_title="A&N Docket Data Extractor",
    page_icon="📄",
    layout="wide"
)

st.title("A&N Docket Data Extractor")

st.markdown("""
Upload one or more docket PDFs.

The application will:

- Extract labour information (operator, times, breaks, travel)
- Extract material tonnages (per load and total)
- Sort PDFs into folders by Material Type and Date
- Generate tracker spreadsheets
- Package everything into a single downloadable ZIP file

When ready, click **Extract Data**.
""")


# =====================================================
# HELPERS
# =====================================================

def format_folder_date(date_str):
    '''
    Converts a date string from DD/MM/YYYY format (as it appears in the docket)
    into DD.MM.YYYY format suitable for folder naming.
    Returns "Unknown Date" if the input is empty, None, or does not match
    the expected format, rather than raising an exception.
    '''
    if not date_str:
        return "Unknown Date"
    try:
        return datetime.strptime(date_str, "%d/%m/%Y").strftime("%d.%m.%Y")
    except ValueError:
        return "Unknown Date"


def convert_duration(text):
    '''
    Converts a plain-English duration string such as "30 Minutes" or "1 Hour"
    into a decimal number of hours (e.g. 0.5 or 1.0).
    Handles combined strings like "1 Hour 30 Minutes" → 1.5.
    The match is case-insensitive so "minutes", "Minutes", and "MINUTES" all work.
    Returns an empty string if the input is empty or None, so that missing
    break/travel times appear as blank cells rather than zero in the spreadsheet.
    Returns 0.0 if text is present but no hour or minute pattern is found.
    '''
    if not text or not text.strip():
        return ""

    text = text.strip()
    total_hours = 0.0

    hour_match = re.search(r"(\d+)\s*hour", text, re.IGNORECASE)
    minute_match = re.search(r"(\d+)\s*min", text, re.IGNORECASE)

    if hour_match:
        total_hours += int(hour_match.group(1))
    if minute_match:
        total_hours += int(minute_match.group(1)) / 60

    return round(total_hours, 2)


def normalise_material(raw_text):
    '''
    Takes the raw material description string extracted from the docket and
    maps it to one of two canonical material names:
      - "Heidelberg Sand"  (catches misspellings like "Heildlberg")
      - "Crushed Rock Basecourse"  (catches "roadbase", "road base")
    Returns an empty string if neither keyword is found, and logs a warning
    via Streamlit so the operator knows extraction was incomplete.
    The comparison is lower-cased so capitalisation differences don't matter.
    '''
    if not raw_text:
        return ""

    lower = raw_text.lower()

    if "sand" in lower or "heid" in lower:
        return "Heidelberg Sand"

    if "roadbase" in lower or "road base" in lower or "road-base" in lower:
        return "Crushed Rock Basecourse"

    return ""


def extract_text_from_pdf(uploaded_file):
    '''
    Opens the uploaded PDF file with pdfplumber and extracts plain text from
    pages 1 and 2 separately.
    Page 1 contains the header information: docket number, operator, date,
    vehicle, and the Time section heading.
    Page 2 contains the actual time values, the items/tonnage section,
    and the signatures.
    Returns a tuple of (page1_text, page2_text).
    If a page does not exist (e.g. a single-page PDF) the corresponding
    string is returned as empty rather than raising an IndexError.
    '''
    with pdfplumber.open(uploaded_file) as pdf:
        page1 = pdf.pages[0].extract_text() or "" if len(pdf.pages) > 0 else ""
        page2 = pdf.pages[1].extract_text() or "" if len(pdf.pages) > 1 else ""
    return page1, page2


def extract_docket_number(page1):
    '''
    Searches page 1 text for the docket number using the pattern
    "Docket A &-D-XXXXX" or "Docket A &-D XXXXX".
    The docket number is the full match including the "A &-D-" prefix
    because that is how it is referenced in other documents.
    Returns an empty string if no match is found.
    '''
    match = re.search(r"Docket\s+(A\s*[&\-]+\s*D[\-\s]+\d+)", page1, re.IGNORECASE)
    if match:
        # Normalise internal whitespace/punctuation for consistency
        raw = match.group(1).strip()
        return raw
    return ""


def extract_date(page1):
    '''
    Searches page 1 text for a date in DD/MM/YYYY format.
    Returns the first match found, which will be the job date that appears
    next to the operator name in the header.
    Returns an empty string if no date is found.
    '''
    match = re.search(r"(\d{2}/\d{2}/\d{4})", page1)
    return match.group(1) if match else ""


def extract_operator(page1):
    '''
    Searches page 1 text for the operator name, which appears on the line
    immediately after the "Operator" label and before the "Date" label.
    The regex captures any non-newline characters between those two labels.
    Returns an empty string if the pattern is not found.
    '''
    match = re.search(r"Operator\s*\n(.+?)\s*\n", page1, re.IGNORECASE)
    if match:
        return match.group(1).strip()

    # Fallback: try matching "Operator <name> Date" on the same line
    match2 = re.search(r"Operator\s+(.+?)\s+Date", page1, re.IGNORECASE)
    if match2:
        return match2.group(1).strip()

    return ""


def extract_times(page1, page2):
    '''
    Extracts start time, end time, break time, and travel time from the docket.

    Strategy:
    The time values (e.g. "05:00 AM", "05:02 PM", "30 Minutes", "1 Hour") appear
    on page 2, on lines immediately following the header row that contains the
    labels "Start Time", "End Time", "Break Time", "Travel Time".

    Because pdfplumber sometimes merges these onto the same line or splits them
    across lines differently depending on the PDF, this function:
      1. Searches for all HH:MM AM/PM patterns to get start and end times.
      2. Searches for duration phrases ("X Hour(s)", "X Minute(s)") to get
         break and travel times, in the order they appear in the text.
         The first duration found is break time; the second is travel time.
         This handles the case where travel time is absent (only one duration found).

    Returns a tuple of (start_time, end_time, break_time_hrs, travel_time_hrs).
    Break and travel are returned as decimal hours via convert_duration(),
    or as empty string if absent.
    '''
    # Combine both pages to ensure we don't miss values split across pages
    combined = page1 + "\n" + page2

    # Extract all HH:MM AM/PM occurrences in order
    time_pattern = re.compile(r"\d{1,2}:\d{2}\s*[AP]M", re.IGNORECASE)
    all_times = time_pattern.findall(combined)

    start_time = all_times[0].strip() if len(all_times) > 0 else ""
    end_time = all_times[1].strip() if len(all_times) > 1 else ""

    # Extract duration phrases in order of appearance
    # Matches things like: "30 Minutes", "1 Hour", "1 Hour 30 Minutes"
    duration_pattern = re.compile(
        r"(\d+\s*(?:hour|hr)s?(?:\s*\d+\s*(?:minute|min)s?)?|\d+\s*(?:minute|min)s?)",
        re.IGNORECASE
    )
    durations = duration_pattern.findall(combined)

    # The first duration is break time, the second is travel time
    break_time = convert_duration(durations[0]) if len(durations) > 0 else ""
    travel_time = convert_duration(durations[1]) if len(durations) > 1 else ""

    return start_time, end_time, break_time, travel_time


def extract_material(page2):
    '''
    Determines the material type delivered in the docket by searching page 2
    text for keywords.
    Checks for sand-related keywords first (including misspellings found in
    real dockets such as "Heildlberg"), then roadbase keywords.
    Passes the matched raw substring through normalise_material() to return
    a canonical material name.
    Returns an empty string if no recognised material keyword is found.
    '''
    lower = page2.lower()

    # Check sand variants — also catches the common "Heildlberg" misspelling
    if "sand" in lower or "heild" in lower or "heid" in lower:
        return normalise_material("sand")

    if "roadbase" in lower or "road base" in lower or "road-base" in lower:
        return normalise_material("roadbase")

    return ""


def extract_total_tonnage(page2):
    '''
    Extracts the cumulative/total tonnage figure from page 2.
    In these dockets the total appears as the first large tonnage number
    on the Items section (e.g. "290.84 T"), which is always the largest
    value on the page because it is the sum of all individual loads.
    Finds all numbers followed by " T" or "T" and returns the maximum,
    which corresponds to the daily total rather than any individual load.
    Returns an empty string if no tonnage figures are found.
    '''
    matches = re.findall(r"(\d+\.\d+)\s*T", page2)
    if not matches:
        return ""
    values = [float(x) for x in matches]
    return max(values)


def extract_individual_tonnages(page2):
    '''
    Extracts the individual load tonnages from page 2.
    In these dockets each individual load is listed as "New Activity  XX.XX T"
    on its own line (or with varying whitespace between the label and figure).
    The regex uses re.DOTALL and allows for newlines between "New Activity"
    and the tonnage number so that line-break variations in pdfplumber output
    are handled correctly.
    Returns a list of floats (one per load), or an empty list if none are found.
    '''
    # Allow any whitespace including newlines between "New Activity" and the number
    matches = re.findall(r"New\s+Activity[\s\S]{0,10}?(\d+\.?\d*)\s*T", page2)
    return [float(x) for x in matches]


def extract_docket(uploaded_file):
    '''
    Master extraction function for a single docket PDF.
    Calls all individual extraction helpers in sequence and assembles
    the results into a single dictionary.
    Catches any exception during processing and returns a failure dictionary
    containing the filename and the error message, so that one bad PDF does
    not abort the entire batch.

    Returns a dict with keys:
      success, pdf_name, date, folder_date, docket, operator, material,
      start_time, end_time, break_time, travel_time,
      tonnages (list), total_tonnage, file_bytes
    On failure:
      success=False, pdf_name, error
    '''
    try:
        page1, page2 = extract_text_from_pdf(uploaded_file)

        docket = extract_docket_number(page1)
        date = extract_date(page1)
        #operator = extract_operator(page1)
        start_time, end_time, break_time, travel_time = extract_times(page1, page2)
        material = extract_material(page2)
        total_tonnage = extract_total_tonnage(page2)
        tonnages = extract_individual_tonnages(page2)

        return {
            "success": True,
            "pdf_name": uploaded_file.name,
            "date": date,
            "folder_date": format_folder_date(date),
            "docket": docket,
            #"operator": operator,
            "material": material,
            "start_time": start_time,
            "end_time": end_time,
            "break_time": break_time,
            "travel_time": travel_time,
            "tonnages": tonnages,
            "total_tonnage": total_tonnage,
            "file_bytes": uploaded_file.getvalue(),
        }

    except Exception as e:
        return {
            "success": False,
            "pdf_name": uploaded_file.name,
            "error": str(e),
        }


def build_row_collections(extracted):
    '''
    Takes the list of successfully extracted docket dictionaries and splits
    them into four row collections used to populate the Excel sheets:
      - labour_rows: one row per docket with time/operator information (removed operator)
      - sand_rows: one row per individual load where material is Heidelberg Sand
      - roadbase_rows: one row per individual load where material is Crushed Rock Basecourse
      - summary_rows: one row per docket with total tonnage and material

    The per-load rows (sand/roadbase) include a "Zone" column left blank so
    the user can fill it in manually after download.

    Returns a tuple of (labour_rows, sand_rows, roadbase_rows, summary_rows).
    '''
    labour_rows = []
    sand_rows = []
    roadbase_rows = []
    summary_rows = []

    for r in extracted:
        labour_rows.append({
            "Date": r["date"],
            "Docket Number": r["docket"],
            #"Operator": r["operator"],
            "Start Time": r["start_time"],
            "End Time": r["end_time"],
            "Break Time (hrs)": r["break_time"],
            "Travel Time (hrs)": r["travel_time"],
        })

        summary_rows.append({
            "Date": r["date"],
            "Docket": r["docket"],
            #"Operator": r["operator"],
            "Material": r["material"],
            "Total Tonnage": r["total_tonnage"],
        })

        for tonnage in r["tonnages"]:
            row = {
                "Date": r["date"],
                "Zone": "",
                "Docket": r["docket"],
                #"Operator": r["operator"],
                "Tonnage": tonnage,
            }
            if r["material"] == "Heidelberg Sand":
                sand_rows.append(row)
            elif r["material"] == "Crushed Rock Basecourse":
                roadbase_rows.append(row)

    return labour_rows, sand_rows, roadbase_rows, summary_rows


def build_excel(labour_rows, sand_rows, roadbase_rows, summary_rows, error_rows):
    '''
    Builds an in-memory Excel workbook (.xlsx) with five sheets:
      1. Labour Tracker       — one row per docket, time and operator data
      2. Heidelberg Sand Tracker      — one row per individual sand load
      3. Crushed Rock Basecourse Tracker — one row per individual roadbase load
      4. Docket Summary       — one row per docket, material and total tonnage
      5. Extraction Errors    — one row per failed PDF with the error message

    Uses pandas ExcelWriter with the openpyxl engine.
    Returns the workbook as a bytes object ready to be written into a ZIP
    or downloaded directly.
    '''
    buffer = io.BytesIO()

    with pd.ExcelWriter(buffer, engine="openpyxl") as writer:
        pd.DataFrame(labour_rows).to_excel(
            writer, sheet_name="Labour Tracker", index=False
        )
        pd.DataFrame(sand_rows).to_excel(
            writer, sheet_name="Heidelberg Sand Tracker", index=False
        )
        pd.DataFrame(roadbase_rows).to_excel(
            writer, sheet_name="Crushed Rock Basecourse Tracker", index=False
        )
        pd.DataFrame(summary_rows).to_excel(
            writer, sheet_name="Docket Summary", index=False
        )
        pd.DataFrame(error_rows).to_excel(
            writer, sheet_name="Extraction Errors", index=False
        )

    buffer.seek(0)
    return buffer.getvalue()


def build_zip(excel_bytes, extracted):
    '''
    Assembles the final ZIP archive containing:
      - Docket_Tracker.xlsx  at the root level
      - Each successfully extracted PDF filed under:
          Sorted PDFs/<Material Type>/<DD.MM.YYYY>/<original_filename.pdf>

    This gives the user a ready-to-use folder structure where PDFs are
    organised first by material type, then by date.

    Returns the ZIP as a bytes object suitable for st.download_button.
    '''
    buffer = io.BytesIO()

    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("Docket_Tracker.xlsx", excel_bytes)

        for item in extracted:
            material_folder = item["material"] if item["material"] else "Unknown Material"
            date_folder = item["folder_date"]
            path = f"Sorted PDFs/{material_folder}/{date_folder}/{item['pdf_name']}"
            zf.writestr(path, item["file_bytes"])

    buffer.seek(0)
    return buffer.getvalue()


def render_preview(extracted, error_rows):
    '''
    Renders an interactive preview of all extracted data directly in the
    Streamlit app before the user downloads the ZIP.
    Shows three expandable sections:
      1. Docket Summary table — date, docket, operator (removed), material, total tonnage
      2. Labour table — date, docket, operator (removed), and all time fields
      3. Errors table — only shown if any PDFs failed to extract

    Uses st.expander so the tables are collapsed by default and don't
    overwhelm the page. Each table is rendered with st.dataframe for
    column sorting and scrolling.
    '''
    if not extracted:
        st.warning("No dockets were successfully extracted.")
        return

    summary_data = [
        {
            "Date": r["date"],
            "Docket": r["docket"],
            #"Operator": r["operator"],
            "Material": r["material"] if r["material"] else "Not detected",
            "Total Tonnage (T)": r["total_tonnage"],
            "Individual Loads": len(r["tonnages"]),
        }
        for r in extracted
    ]

    labour_data = [
        {
            "Date": r["date"],
            "Docket": r["docket"],
            #"Operator": r["operator"],
            "Start Time": r["start_time"],
            "End Time": r["end_time"],
            "Break (hrs)": r["break_time"],
            "Travel (hrs)": r["travel_time"],
        }
        for r in extracted
    ]

    with st.expander("Docket Summary", expanded=True):
        st.dataframe(pd.DataFrame(summary_data), use_container_width=True)

    with st.expander("Labour Tracker", expanded=False):
        st.dataframe(pd.DataFrame(labour_data), use_container_width=True)

    if error_rows:
        with st.expander(f"Extraction Errors ({len(error_rows)})", expanded=True):
            st.dataframe(pd.DataFrame(error_rows), use_container_width=True)


def render_metrics(extracted, error_rows):
    '''
    Renders a row of summary metric cards at the top of the results section.
    Shows:
      - Total PDFs processed (success + failure)
      - Number successfully extracted
      - Number failed
      - Total tonnage across all dockets combined
      - Count of dockets by material type (Sand vs Roadbase)

    Uses st.metric for a clean dashboard-style display.
    '''
    total = len(extracted) + len(error_rows)
    total_tonnage = sum(
        r["total_tonnage"] for r in extracted
        if isinstance(r["total_tonnage"], (int, float))
    )
    sand_count = sum(1 for r in extracted if r["material"] == "Heidelberg Sand")
    roadbase_count = sum(1 for r in extracted if r["material"] == "Crushed Rock Basecourse")
    unknown_count = sum(1 for r in extracted if not r["material"])

    col1, col2, col3, col4, col5, col6, col7 = st.columns(7)
    col1.metric("PDFs Uploaded", total)
    col2.metric("Extracted", len(extracted))
    col3.metric("Failed", len(error_rows))
    col4.metric("Total Tonnage (T)", f"{total_tonnage:,.2f}")
    col5.metric("Sand Dockets", sand_count)
    col6.metric("Roadbase Dockets", roadbase_count)
    col7.metric("Unknown Material", unknown_count)


# =====================================================
# MAIN APP
# =====================================================

def main():
    '''
    Main Streamlit entry point. Renders the file uploader, handles the
    "Extract Data" button click, runs extraction across all uploaded PDFs
    with a progress bar, then:
      1. Displays metric cards summarising the batch
      2. Displays interactive preview tables (summary, labour, errors)
      3. Builds the Excel workbook and ZIP archive
      4. Offers a download button for the ZIP

    All processing logic is delegated to the helper functions above so
    this function stays readable and easy to modify.
    '''
    uploaded_files = st.file_uploader(
        "Upload Docket PDFs",
        type=["pdf"],
        accept_multiple_files=True,
    )

    if not uploaded_files:
        return

    st.info(f"{len(uploaded_files)} PDF(s) loaded. Click **Extract Data** to begin.")

    if not st.button("Extract Data"):
        return

    # ---- Run extraction ----
    extracted = []
    error_rows = []
    progress = st.progress(0)
    status = st.empty()

    for i, uploaded_file in enumerate(uploaded_files):
        status.text(f"Processing {uploaded_file.name} …")
        result = extract_docket(uploaded_file)

        if result["success"]:
            extracted.append(result)

            # Warn immediately if material was not detected
            if not result["material"]:
                st.warning(
                    f"Material type not detected in **{uploaded_file.name}**. "
                    "It will be filed under 'Unknown Material' in the ZIP."
                )
        else:
            error_rows.append({
                "PDF": result["pdf_name"],
                "Error": result["error"],
            })

        progress.progress((i + 1) / len(uploaded_files))

    status.empty()
    progress.empty()

    # ---- Results ----
    st.success(
        f"Extraction complete — {len(extracted)} succeeded, {len(error_rows)} failed."
    )

    render_metrics(extracted, error_rows)
    render_preview(extracted, error_rows)

    # ---- Build outputs ----
    labour_rows, sand_rows, roadbase_rows, summary_rows = build_row_collections(extracted)
    excel_bytes = build_excel(labour_rows, sand_rows, roadbase_rows, summary_rows, error_rows)
    zip_bytes = build_zip(excel_bytes, extracted)

    st.download_button(
        label="📥 Download Processed ZIP",
        data=zip_bytes,
        file_name="Processed_Dockets.zip",
        mime="application/zip",
    )


main()
