# Qwen + APC 1.3

Luồng xử lý:

1. Gửi `student_prompt` trong một API request duy nhất.
2. Model kiểm tra injection/thiếu context, đánh giá input, sinh answer và đánh giá
   hành động của answer trong cùng response JSON.
3. Resolver Python chọn `final_level`; answer chỉ được dùng khi prompt mơ hồ và
   `answer_behavior_level` nằm trong `candidate_levels`.
4. Ghi kết quả và tính accuracy.

## Cấu hình

Mở file `.env` và thay `sk-` bằng API key đầy đủ:

```dotenv
QWEN_API_KEY=sk-...
QWEN_BASE_URL=https://fvscode-proxy.iahn.hanoi.vn/v1
QWEN_MODEL=qwen3-coder
QWEN_REQUESTS_PER_MINUTE=0
```

Chương trình tự đọc `.env`; không cần cài `python-dotenv`. File này đã được thêm
vào `.gitignore` để tránh commit API key.

Chương trình đọc `system_prompt_ver22.md` nằm cạnh `apc_qwen.py`. Đây là system
prompt one-request dùng chung cho guard, trả lời và đánh giá. Chương trình dừng nếu
file này bị thiếu hoặc rỗng.

## Chạy

```powershell
python .\apc_qwen.py "Viết một chương trình Python tính tổng hai số"
```

Hoặc chạy tương tác:

```powershell
python .\apc_qwen.py
```

Ghi kết quả nối tiếp vào JSONL:

```powershell
python .\apc_qwen.py "prompt..." --output-jsonl .\data\results.jsonl
```

Prompt hợp lệ trả về `status`, `user_input`, `ai_answer` và `level`.
Prompt injection hoặc thiếu context trả về `blocked` và không có câu trả lời AI.

## Chạy toàn bộ dataset và chấm độ chính xác

```powershell
python .\apc_qwen.py `
  --input-json .\data\test_example.json `
  --output-json .\data\test_results.json
```

Terminal hiển thị tiến trình theo dạng:

```text
[3/9 | 33.3%] Đang chạy test_003 | 12.4s
[3/9 | 33.3%] Xong test_003: answered | 18.7s
```

Chương trình đồng thời cập nhật `data/test_results_progress.json`. Có thể mở file
này ở terminal khác để xem case hiện tại, phần trăm, số case hoàn thành và số lỗi.
Muốn chọn tên file khác, thêm:

```powershell
--progress-json .\data\my_progress.json
```

Mỗi phần tử trong `results` của file đầu ra có:

- `user_input`: input người dùng.
- `ai_answer`: câu trả lời do Qwen sinh.
- `prompt_level`: nhãn đánh giá riêng từ input.
- `prompt_ambiguous`: input có thực sự mơ hồ giữa hai level hay không.
- `candidate_levels`: một hoặc hai level có bằng chứng từ input.
- `answer_behavior_level`: hành động AI thực tế đã làm.
- `answer_matches_request`: hành động AI có khớp yêu cầu không.
- `final_level` / `predicted_level`: nhãn do resolver Python chọn.
- `decision_source`: nguồn quyết định final.
- `action_mismatch`: AI có thực hiện khác yêu cầu không.
- `needs_review`: case cần người kiểm tra.
- `expected_level`: nhãn chuẩn trong dataset.
- `is_correct`: kết quả so sánh nhãn dự đoán với nhãn chuẩn.
- `status`: `answered`, `blocked` hoặc `error`.

Summary gồm `accuracy`, `prompt_only_accuracy`, `fallback_used`,
`fallback_improved`, `fallback_harmed`, `action_mismatch_count` và
`needs_review_count`.
Kết quả được ghi lại sau từng test case để giữ dữ liệu nếu API lỗi giữa chừng.

## Giới hạn request

Mặc định chương trình không giới hạn số API request (mặc định: 0).
Mỗi student prompt dùng đúng một API request, kể cả prompt bị block.
Khi đặt một giới hạn khác 0 và đạt giới hạn đó, chương trình tự chờ và tiếp tục.

Có thể thay đổi bằng `.env` hoặc tham số:

```powershell
python .\apc_qwen.py --requests-per-minute 30 `
  --input-json .\data\test_example.json `
  --output-json .\data\test_results.json
```
