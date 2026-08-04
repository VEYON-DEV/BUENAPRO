import type { ApplicationData, ApplicationItem } from "../../model/types";
import styles from "./QuoteEditor.module.css";

export function QuoteEditor({
  data,
  onItem,
  onDraft,
}: {
  data: ApplicationData;
  onItem: (item: ApplicationItem, patch: Partial<ApplicationItem>) => void;
  onDraft: (patch: Partial<ApplicationData>) => void;
}) {
  return (
    <div className={styles.editor}>
      <section aria-labelledby="items-title">
        <div className={styles.sectionHeading}>
          <div>
            <h2 id="items-title">Ítems y precios</h2>
            <p>
              Selecciona lo que cotizarás. El total se calcula automáticamente.
            </p>
          </div>
          <strong>
            {data.items.filter((i) => i.selected).length} seleccionados
          </strong>
        </div>
        <div className={styles.tableWrap}>
          <table>
            <thead>
              <tr>
                <th>Incluir</th>
                <th>Descripción</th>
                <th>Cantidad</th>
                <th>Moneda</th>
                <th>Precio unitario</th>
                <th>Total</th>
              </tr>
            </thead>
            <tbody>
              {data.items.map((item) => (
                <tr key={item.id}>
                  <td data-label="Incluir">
                    <input
                      aria-label={`Incluir ${item.description}`}
                      type="checkbox"
                      checked={item.selected}
                      onChange={(e) =>
                        onItem(item, { selected: e.target.checked })
                      }
                    />
                  </td>
                  <td data-label="Descripción">
                    <strong>{item.description}</strong>
                    <span>{item.unit}</span>
                  </td>
                  <td data-label="Cantidad">{item.quantity}</td>
                  <td data-label="Moneda">{item.currency}</td>
                  <td data-label="Precio unitario">
                    <label
                      className={styles.srOnly}
                      htmlFor={`price-${item.id}`}
                    >
                      Precio unitario de {item.description}
                    </label>
                    <input
                      id={`price-${item.id}`}
                      inputMode="decimal"
                      type="number"
                      min="0"
                      step="0.01"
                      disabled={!item.selected}
                      value={item.unitPrice ?? ""}
                      onChange={(e) =>
                        onItem(item, {
                          unitPrice:
                            e.target.value === ""
                              ? null
                              : Number(e.target.value),
                        })
                      }
                    />
                  </td>
                  <td data-label="Total">
                    <strong>
                      {item.selected && item.unitPrice != null
                        ? new Intl.NumberFormat("es-PE", {
                            style: "currency",
                            currency: "PEN",
                          }).format(item.quantity * item.unitPrice)
                        : "—"}
                    </strong>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
        {!data.items.length ? (
          <p className={styles.empty}>
            SEACE no devolvió ítems para esta cotización.
          </p>
        ) : null}
      </section>
      <section aria-labelledby="contact-title">
        <div className={styles.sectionHeading}>
          <div>
            <h2 id="contact-title">Vigencia y contacto</h2>
            <p>
              Datos que recibirá la entidad para comunicarse con tu empresa.
            </p>
          </div>
        </div>
        <div className={styles.fields}>
          <label>
            Vigencia de la cotización
            <input
              type="date"
              value={data.validity ?? ""}
              onChange={(e) => onDraft({ validity: e.target.value })}
            />
          </label>
          <label>
            Correo de contacto
            <input
              type="email"
              autoComplete="email"
              value={data.contactEmail ?? ""}
              onChange={(e) => onDraft({ contactEmail: e.target.value })}
            />
          </label>
          <label>
            Celular de contacto
            <input
              type="tel"
              autoComplete="tel"
              value={data.contactPhone ?? ""}
              onChange={(e) => onDraft({ contactPhone: e.target.value })}
            />
          </label>
        </div>
      </section>
    </div>
  );
}
