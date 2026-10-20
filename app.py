import io, os, re, json, csv, zipfile
from pathlib import Path
from PIL import Image, ImageOps
import streamlit as st
from openai import OpenAI

APP_TITLE = "Fabricademy Photo → Markdown"

st.set_page_config(page_title=APP_TITLE, page_icon="🧵", layout="centered")
st.title("🧵 Fabricademy Photo → Markdown")
st.write("Upload a batch of photos. AI suggests descriptive filenames, captions and alt text; the app compresses the images and packages everything for your GitLab site.")

with st.sidebar:
    st.header("Settings")
    try:
        saved_api_key = st.secrets.get("OPENAI_API_KEY", "")
    except Exception:
        saved_api_key = ""
    if saved_api_key:
        api_key = saved_api_key
        st.success("API key configured")
    else:
        api_key = st.text_input("OpenAI API key", type="password", help="For deployment, save this as OPENAI_API_KEY in the app's secrets so you don't have to enter it each time.")
    model = st.selectbox("Vision model", ["gpt-4.1-mini", "gpt-4.1"], index=0)
    st.caption("API usage is billed separately from a ChatGPT subscription.")
    st.divider()
    st.caption("Compression targets")
    target_kb = st.number_input("Target size per image (KB)", min_value=30, max_value=300, value=150, step=10)
    hard_limit_kb = st.number_input("Hard limit per image (KB)", min_value=100, max_value=1000, value=300, step=25)
    max_edge = st.number_input("Maximum image dimension (px)", min_value=800, max_value=5000, value=2200, step=100)

week = st.text_input("Week / project context", placeholder="e.g. Week 05 — E-textiles")
context = st.text_area("Optional context for the AI", placeholder="What were you making, testing, or documenting? Add details the photos alone may not reveal.", height=90)
uploaded = st.file_uploader("Upload photos", type=["jpg", "jpeg", "png", "webp", "gif", "bmp", "tif", "tiff"], accept_multiple_files=True)

def slugify(value, max_len=70):
    value = value.lower().strip()
    value = re.sub(r"[^a-z0-9]+", "-", value)
    value = re.sub(r"-+", "-", value).strip("-")
    return (value[:max_len].rstrip("-") or "fabricademy-photo")

def unique_name(name, used):
    stem, ext = os.path.splitext(name)
    candidate = name
    n = 2
    while candidate.lower() in used:
        candidate = f"{stem}-{n}{ext}"
        n += 1
    used.add(candidate.lower())
    return candidate

def image_to_jpeg_bytes(image, quality, max_edge):
    image = ImageOps.exif_transpose(image)
    if image.mode in ("RGBA", "LA") or (image.mode == "P" and "transparency" in image.info):
        rgba = image.convert("RGBA")
        background = Image.new("RGB", rgba.size, "white")
        background.paste(rgba, mask=rgba.getchannel("A"))
        image = background
    else:
        image = image.convert("RGB")
    image.thumbnail((int(max_edge), int(max_edge)), Image.Resampling.LANCZOS)
    output = io.BytesIO()
    image.save(output, format="JPEG", quality=quality, optimize=True, progressive=True)
    return output.getvalue(), image

def compress_image(image):
    target = int(target_kb * 1024)
    hard = int(hard_limit_kb * 1024)
    edge = int(max_edge)
    last = b""
    for _ in range(6):
        for quality in (85, 78, 70, 62, 55, 48, 42, 36):
            data, image_out = image_to_jpeg_bytes(image.copy(), quality, edge)
            last = data
            if len(data) <= target:
                return data, "OK", image_out.size
        if edge <= 700:
            break
        edge = max(700, int(edge * 0.82))
    if last and len(last) <= hard:
        return last, "ABOVE_TARGET", image_out.size
    return last, "OVER_HARD_LIMIT", image_out.size

def describe_image(client, model, image_bytes, original_name, week, context):
    import base64
    encoded = base64.b64encode(image_bytes).decode("ascii")
    prompt = f"""
You are helping document an artist-maker's Fabricademy process for a public project website.
Describe only what is visibly supported by the image. Do not invent people’s names, materials, tools, actions, or outcomes.
Use the original filename only as a weak clue, not as proof.
Project/week: {week or "Fabricademy documentation"}
Additional context from the user: {context or "None supplied"}
Original filename: {original_name}

Return ONLY a valid JSON object with exactly these keys:
- "filename": descriptive, specific, lowercase kebab-case filename WITHOUT extension, max 8 words; avoid generic names like image/photo/final
- "caption": natural, concise website caption, 1 sentence; distinguish observation from uncertain inference
- "alt_text": accessible alt text describing the relevant visual content, around 8–25 words; don't begin with "image of"
- "tags": array of 2–5 short topic tags
If the image's content is unclear, use a cautious filename and caption rather than guessing.
"""
    response = client.responses.create(
        model=model,
        input=[{
            "role": "user",
            "content": [
                {"type": "input_text", "text": prompt},
                {"type": "input_image", "image_url": f"data:image/jpeg;base64,{encoded}", "detail": "low"}
            ]
        }]
    )
    text_out = response.output_text.strip()
    text_out = re.sub(r"^```(?:json)?\s*|\s*```$", "", text_out, flags=re.I)
    match = re.search(r"\{.*\}", text_out, flags=re.S)
    if not match:
        raise ValueError("The AI did not return a JSON description.")
    result = json.loads(match.group(0))
    result["filename"] = slugify(result.get("filename", "fabricademy-photo"))
    result["caption"] = str(result.get("caption", "")).strip()
    result["alt_text"] = str(result.get("alt_text", "")).strip()
    tags = result.get("tags", [])
    result["tags"] = [str(t).strip() for t in tags if str(t).strip()][:5] if isinstance(tags, list) else []
    return result

def markdown_escape(text):
    return str(text).replace("\\", "\\\\").replace("[", "\\[").replace("]", "\\]")

st.info("Tip: include the week or project context above. AI can describe visible details, but it cannot reliably infer the full story from a photo alone.")

if uploaded:
    st.write(f"**{len(uploaded)} photo(s) selected.**")
    with st.expander("Preview selected photos"):
        cols = st.columns(3)
        for i, f in enumerate(uploaded):
            try:
                f.seek(0)
                img = Image.open(f)
                cols[i % 3].image(img, caption=f.name, use_container_width=True)
            except Exception:
                st.warning(f"Could not preview {f.name}.")
            finally:
                f.seek(0)

run = st.button("✨ Process photos and prepare ZIP", type="primary", disabled=(not uploaded or not api_key.strip()))
if run:
    if hard_limit_kb < target_kb:
        st.error("The hard limit must be greater than or equal to the target size.")
        st.stop()
    client = OpenAI(api_key=api_key.strip())
    output_files = {}
    manifest_rows = []
    markdown_parts = [f"# {week or 'Fabricademy photo documentation'}", "", "Generated image references. Review captions and filenames before publishing.", ""]
    used_names = set()
    progress = st.progress(0)
    status = st.empty()
    errors = []
    for idx, uploaded_file in enumerate(uploaded):
        status.write(f"Processing {idx + 1}/{len(uploaded)}: {uploaded_file.name}")
        try:
            uploaded_file.seek(0)
            original = Image.open(uploaded_file)
            original.load()

            # Step 1: Compress first. The original upload remains unchanged in memory.
            compressed, compression_status, dimensions = compress_image(original)
            output_image = Image.open(io.BytesIO(compressed))
            output_image.load()

            # Step 2: Send the already-compressed output to the vision model.
            # This keeps the AI analysis consistent with the image that will be published.
            preview_buf = io.BytesIO()
            output_image.convert("RGB").save(preview_buf, format="JPEG", quality=78, optimize=True)
            details = describe_image(client, model, preview_buf.getvalue(), uploaded_file.name, week, context)

            out_name = unique_name(details["filename"] + ".jpg", used_names)
            output_files[f"images/{out_name}"] = compressed
            manifest_rows.append({
                "original_filename": uploaded_file.name,
                "output_filename": out_name,
                "caption": details["caption"],
                "alt_text": details["alt_text"],
                "tags": "; ".join(details["tags"]),
                "width_px": dimensions[0],
                "height_px": dimensions[1],
                "size_kb": round(len(compressed) / 1024, 1),
                "compression_status": compression_status,
            })
            markdown_parts += [
                f"![{markdown_escape(details['alt_text'])}](images/{out_name})",
                "",
                f"*{details['caption']}*",
                ""
            ]
            if compression_status == "OVER_HARD_LIMIT":
                errors.append(f"{out_name} could not be reduced below {hard_limit_kb} KB; output is {len(compressed)/1024:.1f} KB.")
        except Exception as e:
            errors.append(f"{uploaded_file.name}: {type(e).__name__}: {e}")
        progress.progress((idx + 1) / len(uploaded))

    if not manifest_rows:
        st.error("No photos were successfully processed. " + " | ".join(errors))
        st.stop()

    output_files["README.md"] = ("\n".join(markdown_parts).strip() + "\n").encode("utf-8")
    csv_buf = io.StringIO()
    fieldnames = ["original_filename", "output_filename", "caption", "alt_text", "tags", "width_px", "height_px", "size_kb", "compression_status"]
    writer = csv.DictWriter(csv_buf, fieldnames=fieldnames)
    writer.writeheader()
    writer.writerows(manifest_rows)
    output_files["photo_manifest.csv"] = csv_buf.getvalue().encode("utf-8-sig")

    notes = [
        "# Before publishing",
        "",
        "- Check that the generated filenames, captions and alt text are accurate.",
        "- Add the contents of `images/` to the appropriate image folder in your GitLab repository.",
        "- Adjust image paths in `README.md` if your site uses a different folder structure.",
        f"- Compression target: {target_kb} KB; hard limit: {hard_limit_kb} KB.",
        "- The original uploads are not included in this ZIP.",
        ""
    ]
    output_files["PUBLISHING_NOTES.md"] = "\n".join(notes).encode("utf-8")

    zip_buf = io.BytesIO()
    with zipfile.ZipFile(zip_buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for name, data in output_files.items():
            zf.writestr(name, data)
    zip_bytes = zip_buf.getvalue()
    st.success(f"Finished: {len(manifest_rows)} photo(s) processed.")
    st.download_button(
        "⬇️ Download processed ZIP",
        data=zip_bytes,
        file_name=f"{slugify(week or 'fabricademy-documentation')}-processed.zip",
        mime="application/zip",
        type="primary"
    )
    st.subheader("Processing summary")
    st.dataframe(manifest_rows, use_container_width=True, hide_index=True)
    if errors:
        st.warning("Some warnings or errors need attention:\n\n" + "\n\n".join(errors))
    else:
        st.caption("No processing errors reported. Please still review the generated descriptions before publishing.")
