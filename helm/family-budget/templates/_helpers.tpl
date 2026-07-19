{{- define "fb.name" -}}family-budget{{- end -}}

{{- define "fb.labels" -}}
app.kubernetes.io/name: {{ include "fb.name" . }}
app.kubernetes.io/instance: {{ .Release.Name }}
{{- end -}}

{{- define "fb.env" -}}
- name: DATABASE_URL
  value: postgresql://budget:$(POSTGRES_PASSWORD)@{{ include "fb.name" . }}-postgres:5432/budget
- name: REDIS_URL
  value: redis://{{ include "fb.name" . }}-redis:6379/0
- name: S3_ENDPOINT
  value: http://{{ include "fb.name" . }}-minio:9000
- name: S3_BUCKET_DOCUMENTS
  value: documents
envFrom:
- secretRef:
    name: {{ .Values.existingSecret }}
{{- end -}}
