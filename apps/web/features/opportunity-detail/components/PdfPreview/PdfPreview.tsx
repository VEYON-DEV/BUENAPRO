"use client";

import { useEffect, useRef, useState } from "react";
import styles from "./PdfPreview.module.css";

export function PdfPreview({
  src,
  title,
  downloadHref,
  previewHref,
}: {
  src: string;
  title: string;
  downloadHref: string;
  previewHref: string;
}) {
  const dialogRef = useRef<HTMLDialogElement>(null);
  const imageRef = useRef<HTMLImageElement>(null);
  const [previewFailed, setPreviewFailed] = useState(false);
  // El visor se carga solo al abrirlo: abrir la ficha no descarga el PDF completo.
  const [viewerOpened, setViewerOpened] = useState(false);
  const viewerSrc = `${previewHref}#toolbar=1&navpanes=0&view=FitH`;
  // Si la miniatura falló antes de hidratar, onError no llega a dispararse.
  useEffect(() => {
    const image = imageRef.current;
    if (image?.complete && image.naturalWidth === 0) setPreviewFailed(true);
  }, []);
  const openViewer = () => {
    setViewerOpened(true);
    dialogRef.current?.showModal();
  };

  return (
    <>
      <div className={styles.previewShell}>
        <div className={styles.toolbar}>
          <span>Primera página</span>
          <button type="button" onClick={openViewer}>
            Ver documento
          </button>
        </div>
        {previewFailed ? (
          <div className={styles.previewFallback} role="status">
            <strong>Vista previa no disponible</strong>
            <span>SEACE no respondió en este momento. Puedes abrir el documento directamente en SEACE.</span>
            <a href={downloadHref} rel="noopener noreferrer" target="_blank">
              Abrir en SEACE
            </a>
          </div>
        ) : (
          <button
            aria-label={`Ampliar primera página de ${title}`}
            className={styles.pageButton}
            type="button"
            onClick={openViewer}
          >
            <img
              alt={`Primera página de ${title}`}
              className={styles.pageImage}
              height="1120"
              loading="lazy"
              ref={imageRef}
              src={src}
              width="792"
              onError={() => setPreviewFailed(true)}
            />
          </button>
        )}
      </div>

      <dialog aria-label={`Documento ${title}`} className={styles.modal} ref={dialogRef}>
        <div className={styles.modalHeader}>
          <div>
            <span>TDR</span>
            <strong>{title}</strong>
          </div>
          <div className={styles.modalActions}>
            <a className={styles.downloadLink} href={downloadHref} rel="noopener noreferrer" target="_blank">
              Abrir en SEACE
            </a>
            <button type="button" onClick={() => dialogRef.current?.close()}>
              Cerrar
            </button>
          </div>
        </div>
        {viewerOpened ? (
          <iframe className={styles.modalPdf} src={viewerSrc} title={`TDR completo ${title}`} />
        ) : null}
      </dialog>
    </>
  );
}
