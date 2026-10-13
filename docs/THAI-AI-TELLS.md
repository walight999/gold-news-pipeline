# Thai AI tells — what makes our posts read as machine-written

Built 2026-10-13 from the 61 live @tradetongkam autoposts of 2026-10-02 → 10-13
(squawk_log), using the method of [blader/humanizer](https://github.com/blader/humanizer)
(Wikipedia "Signs of AI writing"): a tell is a *default choice* — a phrase that
fits every story, so it says nothing about this one. Counts = posts containing it.

Used by: `src/tweet_writer.py` (`THAI_TELLS` in the autopost prompt +
`thai_tells()` count in the POSTED log line), the `humanizer` skill and the
`no-ai-slop` rule for any Thai public copy.

## 1. The closer formula (strongest tell)

| Tell | Count | Example (live) | Instead |
|---|---|---|---|
| "…ดอลลาร์ → ทอง" in the last sentence | **36/61** | "ดอลลาร์ได้แรงหนุน กดทองในระยะสั้น" | End on the fact, the next data point, or what the market will watch. Mention gold only when the story is about gold. |
| "ในระยะสั้น" / "ในกรอบสั้น" | 17/61 | "เป็นแรงกดต่อราคาทองในระยะสั้น" | Drop it, or give the actual horizon ("ก่อน CPI คืนพุธ"). |
| "ในจังหวะนี้" / "ในช่วงนี้" / "ในตอนนี้" (sentence end) | 7/61 | "ทองได้แรงหนุนในจังหวะนี้" | Drop it. |
| "ทิศทางนี้" / "ในทิศนี้" | 7/61 | "ทองเผชิญแรงกดในทิศนี้" | Name the thing ("ยีลด์ที่ขึ้น"). |

## 2. Stock force verbs

| Tell | Count | Instead |
|---|---|---|
| "ได้/มี/รับแรงหนุน" | 19/61 | Say what moved and by how much, or "อาจหนุน" if it's a may. |
| "เผชิญ/รับ/เป็นแรงกด" | 13/61 | Same. |
| "ทางอ้อม" | 12/61 | If the link is indirect enough to need the word, the story is usually a SKIP. |
| "ทองได้อานิสงส์" / "ได้ประโยชน์" | 2/61 | Drop. |

## 3. Staging instead of stating (humanizer §1–§4)

| Tell | Count | Example | Instead |
|---|---|---|---|
| "ส่งสัญญาณ" as the main verb | 14/61 | "Musalem ส่งสัญญาณ hawkish ชัดขึ้น" | Quote what was said: "Musalem บอกว่าต้องดึงเงินเฟ้อกลับ 2% โดยเร็ว". |
| "สัญญาณ hawkish/dovish (แบบนี้)" | 9/61 | "สัญญาณ dovish แบบนี้เปิดทางให้…" | Let the quote speak; label at most once, in Thai ("สายเข้ม/สายผ่อนคลาย"). |
| "เปิดทาง/เปิดช่อง/เปิดโอกาส/เปิดพื้นที่ให้" | 9/61 | "เปิดทางให้ ECB ลดดอกเบี้ยต่อ" | "ทำให้ ECB ลดดอกเบี้ยได้ง่ายขึ้น" or cut. |
| "ซึ่งเป็น(ปัจจัย/แรง/สภาพแวดล้อม)…" tail | 5/61 | "…ซึ่งเป็นสภาพแวดล้อมที่ทองได้ประโยชน์" | Cut the tail clause. |
| "อย่างมีนัยสำคัญ" / "ชัดเจน" as intensifier | 5/61 | "ต่ำกว่าคาดอย่างมีนัยสำคัญ" | Give the gap ("ต่ำกว่าคาด 0.7 จุด"). |
| "(สัญญาณ/ท่าที/ตัวเลข)แบบนี้" | 7/61 | "ตัวเลขอ่อนแบบนี้เปิดทาง…" | Name it once, then move on. |
| "สะท้อน(ว่า)" | 2/61 | — | Use only for a reporter's reading, attributed. |

## 4. Rules of thumb (from humanizer, adapted)

- Every sentence must add a fact the reader didn't have. A closing sentence that
  only restates direction ("ทองมีแรงกด") adds nothing → cut it.
- Vary the shape: not every post is fact → interpretation → gold. Some posts are
  just the number and the next thing to watch.
- At most 2 of the tells above per post. 3+ reads as a template.
- Never fix a tell by inventing a detail. If the source gives no number or
  horizon, write a shorter sentence.
