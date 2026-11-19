import { useEffect, useId, useRef, useState } from "react";
import { api } from "../api/client";
import type { Domain, Many2oneValue } from "../api/types";
import { t } from "../i18n";

interface Props {
  comodel: string;
  value: Many2oneValue | null;
  onChange: (value: Many2oneValue | null) => void;
  domain?: Domain;
  disabled?: boolean;
  required?: boolean;
  label: string;
  id?: string;
  allowCreate?: boolean;
}

/** Accessible combobox searching the comodel with name_search (debounced). */
export function Many2oneInput({ comodel, value, onChange, domain, disabled, required, label, id, allowCreate }: Props) {
  const [text, setText] = useState(value?.display_name ?? "");
  const [open, setOpen] = useState(false);
  const [options, setOptions] = useState<Many2oneValue[]>([]);
  const [active, setActive] = useState(0);
  const listId = useId();
  const timer = useRef<number | undefined>(undefined);

  useEffect(() => setText(value?.display_name ?? ""), [value?.id, value?.display_name]);

  const search = (q: string) => {
    window.clearTimeout(timer.current);
    timer.current = window.setTimeout(() => {
      api
        .nameSearch(comodel, q, domain)
        .then((rows) => {
          setOptions(rows);
          setActive(0);
          setOpen(true);
        })
        .catch(() => setOptions([]));
    }, 180);
  };

  const choose = async (option: Many2oneValue | "create") => {
    setOpen(false);
    if (option === "create") {
      const created = await api.create(comodel, { name: text }, ["display_name"]);
      onChange({ id: Number(created.id), display_name: String(created.display_name ?? text) });
      return;
    }
    onChange(option);
    setText(option.display_name ?? "");
  };

  const items: (Many2oneValue | "create")[] = [
    ...options,
    ...(allowCreate && text.trim() ? (["create"] as const) : []),
  ];

  return (
    <div className="m2o">
      <input
        id={id}
        role="combobox"
        aria-label={label}
        aria-expanded={open}
        aria-controls={listId}
        aria-autocomplete="list"
        aria-activedescendant={open && items[active] ? `${listId}-${active}` : undefined}
        aria-required={required}
        className="input"
        value={text}
        disabled={disabled}
        autoComplete="off"
        onChange={(e) => {
          setText(e.target.value);
          search(e.target.value);
          if (!e.target.value) onChange(null);
        }}
        onFocus={() => !disabled && search(text === value?.display_name ? "" : text)}
        onBlur={() =>
          window.setTimeout(() => {
            setOpen(false);
            setText(value?.display_name ?? "");
          }, 150)
        }
        onKeyDown={(e) => {
          if (!open) return;
          if (e.key === "ArrowDown") {
            e.preventDefault();
            setActive((a) => Math.min(a + 1, items.length - 1));
          } else if (e.key === "ArrowUp") {
            e.preventDefault();
            setActive((a) => Math.max(a - 1, 0));
          } else if (e.key === "Enter" && items[active]) {
            e.preventDefault();
            void choose(items[active]);
          } else if (e.key === "Escape") {
            setOpen(false);
          }
        }}
      />
      {open && items.length > 0 && (
        <ul className="m2o-menu" role="listbox" id={listId}>
          {items.map((option, index) => (
            <li
              key={option === "create" ? "__create" : option.id}
              id={`${listId}-${index}`}
              role="option"
              aria-selected={index === active}
              className={index === active ? "active" : undefined}
              onMouseDown={(e) => {
                e.preventDefault();
                void choose(option);
              }}
            >
              {option === "create" ? <em>{t("Create “{name}”", { name: text })}</em> : option.display_name}
            </li>
          ))}
        </ul>
      )}
    </div>
  );
}
