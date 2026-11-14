"use client";

import { useMutation, useQueryClient } from "@tanstack/react-query";
import { Loader2 } from "lucide-react";
import { useState } from "react";
import { useApi } from "@/components/providers";
import { Button } from "@/components/ui/button";
import { Dialog, DialogContent } from "@/components/ui/dialog";
import { Field, Textarea } from "@/components/ui/input";
import { Switch } from "@/components/ui/switch";
import { splitBatch } from "@/lib/batch";
import { DOWNLOADS } from "@/lib/events";
import { useI18n } from "@/lib/i18n";

function Form({ onDone }: { onDone: () => void }) {
  const { t } = useI18n();
  const api = useApi();
  const client = useQueryClient();
  const [text, setText] = useState("");
  const [paused, setPaused] = useState(false);
  const parts = splitBatch(text);
  const add = useMutation({
    mutationFn: () => api.batch({ ...parts, start_paused: paused }),
    onSuccess: () => void client.invalidateQueries({ queryKey: DOWNLOADS }),
  });
  const ready = parts.urls.length + parts.patterns.length > 0 && !add.isPending;
  return (
    <form
      className="flex min-h-0 flex-col"
      onSubmit={(event) => {
        event.preventDefault();
        if (ready) add.mutate();
      }}
    >
      <div className="grid gap-4 overflow-y-auto px-5 py-4">
        <Field label={t("batch.links")} htmlFor="batch-links">
          <Textarea
            id="batch-links"
            dir="ltr"
            rows={10}
            autoFocus
            spellCheck={false}
            className="font-mono text-xs"
            placeholder={"https://example.com/file[001-100].jpg\nhttps://example.com/other.zip"}
            value={text}
            onChange={(event) => {
              setText(event.target.value);
              add.reset();
            }}
          />
        </Field>
        <label className="flex items-center gap-2 text-sm">
          <Switch checked={paused} onCheckedChange={setPaused} />
          {t("add.start_paused")}
        </label>
        {add.data ? (
          <p role="status" className="text-sm text-success">
            {t("batch.added", { count: add.data.ids.length })}
          </p>
        ) : null}
        {add.error ? <p className="text-sm text-danger">{add.error.message}</p> : null}
      </div>
      <div className="flex justify-end gap-2 border-t px-5 py-3">
        <Button onClick={onDone}>{add.data ? t("action.close") : t("action.cancel")}</Button>
        <Button type="submit" variant="primary" disabled={!ready}>
          {add.isPending ? <Loader2 className="animate-spin" /> : null}
          {t("batch.submit")}
        </Button>
      </div>
    </form>
  );
}

export function BatchDialog({ open, onClose }: { open: boolean; onClose: () => void }) {
  const { t } = useI18n();
  return (
    <Dialog open={open} onOpenChange={(next) => (next ? undefined : onClose())}>
      <DialogContent title={t("batch.title")} description={t("batch.body")}>
        {open ? <Form onDone={onClose} /> : null}
      </DialogContent>
    </Dialog>
  );
}
