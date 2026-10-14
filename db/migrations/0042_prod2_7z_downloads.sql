-- 7z originals are only downloaded by an explicitly frozen archive policy.
-- Existing downloaded revisions, source identities and receipts are unchanged.
ALTER TABLE prod2_document_downloads
  DROP CONSTRAINT prod2_document_downloads_format_check;
ALTER TABLE prod2_document_downloads
  ADD CONSTRAINT prod2_document_downloads_format_check
  CHECK (format IN ('pdf', 'zip', 'ole', 'rar', '7z'));
