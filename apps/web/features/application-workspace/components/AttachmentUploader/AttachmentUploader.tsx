"use client";

import { useRef, useState } from "react";
import type { ApplicationAttachment } from "../../model/types";
import styles from "./AttachmentUploader.module.css";

const ACCEPTED = ".pdf,.doc,.docx,.xls,.xlsx";

function fileSize(bytes?: number) {
  if (!bytes) return "";
  return bytes < 1024 * 1024
    ? `${Math.ceil(bytes / 1024)} KB`
    : `${(bytes / 1024 / 1024).toFixed(1)} MB`;
}

export function AttachmentUploader({
  attachments,
  uploading,
  onUpload,
  onDelete,
}: {
  attachments: ApplicationAttachment[];
  uploading: boolean;
  onUpload: (file: File) => void;
  onDelete: (id: string) => void;
}) {
  const input = useRef<HTMLInputElement>(null);
  const [dragging, setDragging] = useState(false);

  function choose(file?: File) {
    if (file) onUpload(file);
    if (input.current) input.current.value = "";
  }

  return (
    <section className={styles.section} aria-labelledby="proposal-files-title">
      <div className={styles.heading}>
        <div>
          <h2 id="proposal-files-title">Tu propuesta y anexos</h2>
          <p>Adjunta aquí los archivos completos que presentarás.</p>
        </div>
        <span>{attachments.length} adjuntos</span>
      </div>
      <button
        className={styles.dropzone}
        data-dragging={dragging}
        disabled={uploading}
        type="button"
        onClick={() => input.current?.click()}
        onDragEnter={(event) => {
          event.preventDefault();
          setDragging(true);
        }}
        onDragOver={(event) => event.preventDefault()}
        onDragLeave={() => setDragging(false)}
        onDrop={(event) => {
          event.preventDefault();
          setDragging(false);
          choose(event.dataTransfer.files[0]);
        }}
      >
        <span className={styles.uploadIcon} aria-hidden="true">
          ↑
        </span>
        <strong>
          {uploading ? "Subiendo archivo…" : "Subir propuesta o anexo"}
        </strong>
        <small>PDF, Word o Excel · máximo 10 MB</small>
      </button>
      <input
        ref={input}
        className={styles.hiddenInput}
        type="file"
        accept={ACCEPTED}
        onChange={(event) => choose(event.target.files?.[0])}
      />
      {attachments.length ? (
        <ul className={styles.files}>
          {attachments.map((file) => (
            <li key={file.id}>
              <span className={styles.fileType} aria-hidden="true">
                {file.name.split(".").pop()?.slice(0, 4).toUpperCase() || "DOC"}
              </span>
              <div>
                <strong>{file.name}</strong>
                <small>{fileSize(file.sizeBytes) || "Archivo adjunto"}</small>
              </div>
              <a href={file.downloadUrl} download>
                Descargar
              </a>
              <button type="button" onClick={() => onDelete(file.id)}>
                Eliminar
              </button>
            </li>
          ))}
        </ul>
      ) : null}
    </section>
  );
}
