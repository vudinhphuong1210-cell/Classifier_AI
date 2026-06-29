# Qwen + APC 1.3

Luồng xử lý:

1. Kiểm tra prompt injection và thiếu context.
2. Nếu gặp một trong hai trường hợp trên, trả về `blocked` và không sinh câu trả lời.
3. Nếu hợp lệ, gọi `qwen3-coder` sinh câu trả lời.
4. Gửi cả input và câu trả lời vào bộ đánh giá APC 1.3 để lấy nhãn L0-L6.

## Cấu hình

Mở file `.env` và thay `sk-` bằng API key đầy đủ:

```dotenv
QWEN_API_KEY=sk-...
QWEN_BASE_URL=https://fvscode-proxy.iahn.hanoi.vn/v1
QWEN_MODEL=qwen3-coder
QWEN_REQUESTS_PER_MINUTE=30
```

Chương trình tự đọc `.env`; không cần cài `python-dotenv`. File này đã được thêm
vào `.gitignore` để tránh commit API key.

Bộ đánh giá đọc trực tiếp toàn bộ nội dung `system_prompt_ver22.md` nằm cạnh
`apc_qwen.py`. Chương trình sẽ dừng nếu không tìm thấy hoặc file prompt bị rỗng.
Có thể chỉ định file khác bằng `--apc-prompt-file`.

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
- `predicted_level`: nhãn APC do Qwen đánh giá từ input và câu trả lời.
- `expected_level`: nhãn chuẩn trong dataset.
- `is_correct`: kết quả so sánh nhãn dự đoán với nhãn chuẩn.
- `status`: `answered`, `blocked` hoặc `error`.

`summary.accuracy` là tỷ lệ dự đoán đúng trên các test case có nhãn kỳ vọng.
Kết quả được ghi lại sau từng test case để giữ dữ liệu nếu API lỗi giữa chừng.

## Giới hạn request

Mặc định chương trình giới hạn tổng cộng 30 API request trong mỗi cửa sổ 60 giây.
Giới hạn áp dụng chung cho cả bước kiểm tra, sinh câu trả lời và đánh giá APC.
Khi đạt giới hạn, chương trình tự chờ và tiếp tục.

Có thể thay đổi bằng `.env` hoặc tham số:

```powershell
python .\apc_qwen.py --requests-per-minute 30 `
  --input-json .\data\test_example.json `
  --output-json .\data\test_results.json
```
