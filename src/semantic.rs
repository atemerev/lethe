use std::collections::hash_map::DefaultHasher;
use std::fs;
use std::future::Future;
use std::hash::{Hash, Hasher};
use std::path::{Path, PathBuf};
use std::sync::{Arc, Mutex};
use std::thread;

use anyhow::{Context, Result, anyhow, bail};
use arrow_array::types::Float32Type;
use arrow_array::{
    Array, ArrayRef, FixedSizeListArray, Float32Array, Float64Array, RecordBatch, StringArray,
};
use arrow_schema::{DataType, Field, Schema};
use fastembed::{
    EmbeddingModel, InitOptions, InitOptionsUserDefined, OutputKey, QuantizationMode,
    TextEmbedding, TokenizerFiles, UserDefinedEmbeddingModel,
};
use futures::TryStreamExt;
use hf_hub::api::sync::ApiBuilder;
use lancedb::database::CreateTableMode;
use lancedb::query::{ExecutableQuery, QueryBase};
use serde::{Deserialize, Serialize};

pub const LEGACY_EMBEDDING_MODEL: &str = "Snowflake/snowflake-arctic-embed-m-v2.0";
pub const LEGACY_EMBEDDING_DIMENSIONS: usize = 768;
pub const VECTOR_COLUMN: &str = "vector";

#[derive(Clone, Debug, Eq, PartialEq, Serialize, Deserialize)]
pub struct SemanticDocument {
    pub id: String,
    pub kind: String,
    pub text: String,
    pub source: String,
    pub tags: Vec<String>,
    pub created_at: String,
}

#[derive(Clone, Debug, PartialEq, Serialize, Deserialize)]
pub struct SemanticHit {
    pub id: String,
    pub kind: String,
    pub text: String,
    pub source: String,
    pub tags: Vec<String>,
    pub created_at: String,
    pub distance: f64,
}

#[derive(Clone, Debug, Eq, PartialEq)]
pub struct SemanticIndexConfig {
    pub enabled: bool,
    pub provider: String,
    pub model: String,
}

impl SemanticIndexConfig {
    pub fn from_env() -> Self {
        Self {
            enabled: env_bool("LETHE_SEMANTIC_SEARCH_ENABLED", true),
            provider: env_string(
                "LETHE_EMBEDDING_PROVIDER",
                if cfg!(test) { "hash" } else { "fastembed" },
            ),
            model: env_string("LETHE_EMBEDDING_MODEL", LEGACY_EMBEDDING_MODEL),
        }
    }
}

#[derive(Clone)]
pub struct EmbeddingEngine {
    embedder: Arc<dyn TextEmbedder>,
}

impl std::fmt::Debug for EmbeddingEngine {
    fn fmt(&self, formatter: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        formatter
            .debug_struct("EmbeddingEngine")
            .finish_non_exhaustive()
    }
}

impl EmbeddingEngine {
    pub fn from_env(cache_root: &Path) -> Self {
        let config = SemanticIndexConfig::from_env();
        Self::from_config(&config, cache_root)
    }

    pub fn from_config(config: &SemanticIndexConfig, cache_root: &Path) -> Self {
        Self {
            embedder: Arc::from(embedder_for_config(config, cache_root)),
        }
    }

    #[cfg(test)]
    pub fn with_hash_dimensions(dimensions: usize) -> Self {
        Self {
            embedder: Arc::new(HashTextEmbedder::new(dimensions)),
        }
    }

    pub fn embed_documents(&self, texts: &[&str]) -> Result<Vec<Vec<f32>>> {
        self.embedder.embed_documents(texts)
    }

    pub fn embed_document(&self, text: &str) -> Result<Vec<f32>> {
        if text.trim().is_empty() {
            return Ok(vec![0.0; LEGACY_EMBEDDING_DIMENSIONS]);
        }
        self.embed_documents(&[text])?
            .into_iter()
            .next()
            .ok_or_else(|| anyhow!("embedding provider returned no document vector"))
    }

    pub fn embed_query(&self, text: &str) -> Result<Vec<f32>> {
        if text.trim().is_empty() {
            return Ok(vec![0.0; LEGACY_EMBEDDING_DIMENSIONS]);
        }
        self.embedder.embed_query(text)
    }
}

#[derive(Debug, Serialize, Deserialize)]
struct SemanticManifest {
    fingerprint: u64,
    dimension: usize,
    count: usize,
}

pub struct SemanticIndex {
    root: PathBuf,
    config: SemanticIndexConfig,
    embedder: Box<dyn TextEmbedder>,
}

impl std::fmt::Debug for SemanticIndex {
    fn fmt(&self, formatter: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        formatter
            .debug_struct("SemanticIndex")
            .field("root", &self.root)
            .field("config", &self.config)
            .finish_non_exhaustive()
    }
}

impl SemanticIndex {
    pub fn new(root: impl Into<PathBuf>) -> Self {
        let root = root.into();
        let config = SemanticIndexConfig::from_env();
        let embedder = embedder_for_config(&config, &root);
        Self {
            root,
            config,
            embedder,
        }
    }

    #[cfg(test)]
    pub fn with_hash_embedder(root: impl Into<PathBuf>, dimensions: usize) -> Self {
        Self {
            root: root.into(),
            config: SemanticIndexConfig {
                enabled: true,
                provider: "hash".to_string(),
                model: format!("hash-{dimensions}"),
            },
            embedder: Box::new(HashTextEmbedder::new(dimensions)),
        }
    }

    pub fn disabled(root: impl Into<PathBuf>) -> Self {
        Self {
            root: root.into(),
            config: SemanticIndexConfig {
                enabled: false,
                provider: "disabled".to_string(),
                model: "disabled".to_string(),
            },
            embedder: Box::new(HashTextEmbedder::new(8)),
        }
    }

    pub fn search(
        &self,
        table: &str,
        documents: &[SemanticDocument],
        query: &str,
        limit: usize,
    ) -> Result<Vec<SemanticHit>> {
        if !self.config.enabled || query.trim().is_empty() || documents.is_empty() {
            return Ok(Vec::new());
        }

        let limit = limit.max(1);
        let table = sanitize_table_name(table);
        self.ensure_table(&table, documents)?;
        let query_vector = self.embedder.embed_query(query.trim())?;
        match self.query_table(&table, query_vector.clone(), limit) {
            Ok(results) => Ok(results),
            Err(error) => {
                tracing::warn!("semantic search table reload failed, rebuilding: {error}");
                self.rebuild_table(&table, documents)?;
                self.query_table(&table, query_vector, limit)
            }
        }
    }

    fn ensure_table(&self, table: &str, documents: &[SemanticDocument]) -> Result<()> {
        fs::create_dir_all(&self.root)?;
        let fingerprint = self.fingerprint(documents);
        if let Some(manifest) = self.load_manifest(table)
            && manifest.fingerprint == fingerprint
            && manifest.count == documents.len()
        {
            return Ok(());
        }
        self.rebuild_table(table, documents)
    }

    fn rebuild_table(&self, table: &str, documents: &[SemanticDocument]) -> Result<()> {
        if documents.is_empty() {
            return Ok(());
        }
        let texts = documents
            .iter()
            .map(|document| document.text.as_str())
            .collect::<Vec<_>>();
        let embeddings = self.embedder.embed_documents(&texts)?;
        let dimension = embeddings
            .first()
            .map(Vec::len)
            .filter(|dimension| *dimension > 0)
            .ok_or_else(|| anyhow!("embedding provider returned no vectors"))?;
        if embeddings
            .iter()
            .any(|embedding| embedding.len() != dimension)
        {
            bail!("embedding provider returned inconsistent vector dimensions");
        }

        let batch = document_batch(documents, embeddings, dimension)?;
        let db_path = self.root.display().to_string();
        let table_name = table.to_string();
        run_lancedb(async move {
            let db = lancedb::connect(&db_path).execute().await?;
            db.create_table(&table_name, batch)
                .mode(CreateTableMode::Overwrite)
                .execute()
                .await?;
            Ok(())
        })?;

        self.write_manifest(
            table,
            &SemanticManifest {
                fingerprint: self.fingerprint(documents),
                dimension,
                count: documents.len(),
            },
        )
    }

    fn query_table(
        &self,
        table: &str,
        query_vector: Vec<f32>,
        limit: usize,
    ) -> Result<Vec<SemanticHit>> {
        let db_path = self.root.display().to_string();
        let table_name = table.to_string();
        let batches = run_lancedb(async move {
            let db = lancedb::connect(&db_path).execute().await?;
            let table = db.open_table(&table_name).execute().await?;
            let stream = table
                .query()
                .nearest_to(query_vector)?
                .limit(limit)
                .execute()
                .await?;
            let batches = stream.try_collect::<Vec<_>>().await?;
            Ok(batches)
        })?;
        hits_from_batches(&batches)
    }

    fn fingerprint(&self, documents: &[SemanticDocument]) -> u64 {
        let mut hasher = DefaultHasher::new();
        self.config.provider.hash(&mut hasher);
        self.config.model.hash(&mut hasher);
        for document in documents {
            document.id.hash(&mut hasher);
            document.kind.hash(&mut hasher);
            document.source.hash(&mut hasher);
            document.created_at.hash(&mut hasher);
            document.text.hash(&mut hasher);
            document.tags.hash(&mut hasher);
        }
        hasher.finish()
    }

    fn manifest_path(&self, table: &str) -> PathBuf {
        self.root.join(format!("{table}.manifest.json"))
    }

    fn load_manifest(&self, table: &str) -> Option<SemanticManifest> {
        let raw = fs::read_to_string(self.manifest_path(table)).ok()?;
        serde_json::from_str(&raw).ok()
    }

    fn write_manifest(&self, table: &str, manifest: &SemanticManifest) -> Result<()> {
        fs::write(
            self.manifest_path(table),
            serde_json::to_string_pretty(manifest)?,
        )?;
        Ok(())
    }
}

pub trait TextEmbedder: Send + Sync {
    fn embed_documents(&self, texts: &[&str]) -> Result<Vec<Vec<f32>>>;
    fn embed_query(&self, text: &str) -> Result<Vec<f32>>;
}

struct FastEmbedTextEmbedder {
    model_name: String,
    cache_dir: PathBuf,
    model: Mutex<Option<TextEmbedding>>,
}

impl FastEmbedTextEmbedder {
    fn new(model_name: impl Into<String>, cache_dir: impl Into<PathBuf>) -> Self {
        Self {
            model_name: model_name.into(),
            cache_dir: cache_dir.into(),
            model: Mutex::new(None),
        }
    }

    fn model(&self) -> Result<std::sync::MutexGuard<'_, Option<TextEmbedding>>> {
        let mut guard = self
            .model
            .lock()
            .map_err(|error| anyhow!("embedding model lock poisoned: {error}"))?;
        if guard.is_none() {
            *guard = Some(if is_legacy_snowflake_model(&self.model_name) {
                legacy_snowflake_embedder(&self.cache_dir)?
            } else {
                let model = parse_fastembed_model(&self.model_name)?;
                let options = InitOptions::new(model)
                    .with_cache_dir(self.cache_dir.clone())
                    .with_show_download_progress(false);
                TextEmbedding::try_new(options)?
            });
        }
        Ok(guard)
    }
}

impl TextEmbedder for FastEmbedTextEmbedder {
    fn embed_documents(&self, texts: &[&str]) -> Result<Vec<Vec<f32>>> {
        let inputs = texts
            .iter()
            .map(|text| text.trim().to_string())
            .collect::<Vec<_>>();
        let mut guard = self.model()?;
        guard
            .as_mut()
            .ok_or_else(|| anyhow!("embedding model unavailable"))?
            .embed(inputs, None)
            .context("failed to embed documents")
    }

    fn embed_query(&self, text: &str) -> Result<Vec<f32>> {
        let mut guard = self.model()?;
        let mut embeddings = guard
            .as_mut()
            .ok_or_else(|| anyhow!("embedding model unavailable"))?
            .embed(vec![format!("query: {}", text.trim())], None)
            .context("failed to embed query")?;
        embeddings
            .pop()
            .ok_or_else(|| anyhow!("embedding provider returned no query vector"))
    }
}

#[derive(Debug)]
struct HashTextEmbedder {
    dimensions: usize,
}

impl HashTextEmbedder {
    fn new(dimensions: usize) -> Self {
        Self {
            dimensions: dimensions.max(8),
        }
    }

    fn embed(&self, text: &str) -> Vec<f32> {
        let mut vector = vec![0.0_f32; self.dimensions];
        for token in text
            .split(|ch: char| !ch.is_alphanumeric())
            .map(str::to_ascii_lowercase)
            .filter(|token| !token.is_empty())
        {
            let mut hasher = DefaultHasher::new();
            token.hash(&mut hasher);
            let hash = hasher.finish();
            let index = hash as usize % self.dimensions;
            let sign = if (hash >> 63) == 0 { 1.0 } else { -1.0 };
            vector[index] += sign;
        }
        normalize(&mut vector);
        vector
    }
}

impl TextEmbedder for HashTextEmbedder {
    fn embed_documents(&self, texts: &[&str]) -> Result<Vec<Vec<f32>>> {
        Ok(texts.iter().map(|text| self.embed(text)).collect())
    }

    fn embed_query(&self, text: &str) -> Result<Vec<f32>> {
        Ok(self.embed(text))
    }
}

fn embedder_for_config(config: &SemanticIndexConfig, root: &Path) -> Box<dyn TextEmbedder> {
    match config.provider.trim().to_ascii_lowercase().as_str() {
        "hash" => Box::new(HashTextEmbedder::new(LEGACY_EMBEDDING_DIMENSIONS)),
        _ => Box::new(FastEmbedTextEmbedder::new(
            config.model.clone(),
            root.join("models"),
        )),
    }
}

fn parse_fastembed_model(model: &str) -> Result<EmbeddingModel> {
    match model.trim().to_ascii_lowercase().as_str() {
        "" | "all-minilm-l6-v2" | "all_minilm_l6_v2" => Ok(EmbeddingModel::AllMiniLML6V2),
        "snowflake/snowflake-arctic-embed-m-v2.0"
        | "snowflake-arctic-embed-m-v2.0"
        | "snowflake/snowflake-arctic-embed-m"
        | "snowflake-arctic-embed-m" => Ok(EmbeddingModel::SnowflakeArcticEmbedMQ),
        "snowflake/snowflake-arctic-embed-m-fp32" | "snowflake-arctic-embed-m-fp32" => {
            Ok(EmbeddingModel::SnowflakeArcticEmbedM)
        }
        other => bail!("unsupported LETHE_EMBEDDING_MODEL for fastembed: {other}"),
    }
}

fn is_legacy_snowflake_model(model: &str) -> bool {
    matches!(
        model.trim().to_ascii_lowercase().as_str(),
        "" | "snowflake/snowflake-arctic-embed-m-v2.0" | "snowflake-arctic-embed-m-v2.0"
    )
}

fn legacy_snowflake_embedder(cache_dir: &Path) -> Result<TextEmbedding> {
    let repo = ApiBuilder::new()
        .with_cache_dir(cache_dir.to_path_buf())
        .build()?
        .model(LEGACY_EMBEDDING_MODEL.to_string());
    let tokenizer_files = TokenizerFiles {
        tokenizer_file: fs::read(repo.get("tokenizer.json")?)?,
        config_file: fs::read(repo.get("config.json")?)?,
        special_tokens_map_file: fs::read(repo.get("special_tokens_map.json")?)?,
        tokenizer_config_file: fs::read(repo.get("tokenizer_config.json")?)?,
    };
    let mut model = UserDefinedEmbeddingModel::new(
        fs::read(repo.get("onnx/model_int8.onnx")?)?,
        tokenizer_files,
    )
    .with_quantization(QuantizationMode::Dynamic);
    model.output_key = Some(OutputKey::ByName("sentence_embedding"));

    TextEmbedding::try_new_from_user_defined(
        model,
        InitOptionsUserDefined::new().with_max_length(512),
    )
}

fn document_batch(
    documents: &[SemanticDocument],
    embeddings: Vec<Vec<f32>>,
    dimension: usize,
) -> Result<RecordBatch> {
    let schema = std::sync::Arc::new(Schema::new(vec![
        Field::new("id", DataType::Utf8, false),
        Field::new("kind", DataType::Utf8, false),
        Field::new("text", DataType::Utf8, false),
        Field::new("source", DataType::Utf8, false),
        Field::new("tags", DataType::Utf8, false),
        Field::new("created_at", DataType::Utf8, false),
        Field::new(
            VECTOR_COLUMN,
            DataType::FixedSizeList(
                std::sync::Arc::new(Field::new("item", DataType::Float32, true)),
                dimension as i32,
            ),
            false,
        ),
    ]));

    let vectors = FixedSizeListArray::from_iter_primitive::<Float32Type, _, _>(
        embeddings
            .into_iter()
            .map(|embedding| Some(embedding.into_iter().map(Some).collect::<Vec<_>>())),
        dimension as i32,
    );

    RecordBatch::try_new(
        schema,
        vec![
            string_array(documents.iter().map(|document| document.id.as_str())),
            string_array(documents.iter().map(|document| document.kind.as_str())),
            string_array(documents.iter().map(|document| document.text.as_str())),
            string_array(documents.iter().map(|document| document.source.as_str())),
            string_array(documents.iter().map(|document| document.tags.join(","))),
            string_array(
                documents
                    .iter()
                    .map(|document| document.created_at.as_str()),
            ),
            std::sync::Arc::new(vectors) as ArrayRef,
        ],
    )
    .context("failed to build semantic record batch")
}

fn string_array<I, S>(values: I) -> ArrayRef
where
    I: IntoIterator<Item = S>,
    S: AsRef<str>,
{
    std::sync::Arc::new(StringArray::from_iter_values(
        values.into_iter().map(|value| value.as_ref().to_string()),
    )) as ArrayRef
}

pub fn utf8_array<I, S>(values: I) -> ArrayRef
where
    I: IntoIterator<Item = S>,
    S: AsRef<str>,
{
    string_array(values)
}

pub fn vector_array(embeddings: Vec<Vec<f32>>, dimension: usize) -> ArrayRef {
    Arc::new(
        FixedSizeListArray::from_iter_primitive::<Float32Type, _, _>(
            embeddings
                .into_iter()
                .map(|embedding| Some(embedding.into_iter().map(Some).collect::<Vec<_>>())),
            dimension as i32,
        ),
    ) as ArrayRef
}

fn hits_from_batches(batches: &[RecordBatch]) -> Result<Vec<SemanticHit>> {
    let mut hits = Vec::new();
    for batch in batches {
        let ids = string_column(batch, "id")?;
        let kinds = string_column(batch, "kind")?;
        let texts = string_column(batch, "text")?;
        let sources = string_column(batch, "source")?;
        let tags = string_column(batch, "tags")?;
        let created = string_column(batch, "created_at")?;
        let distances = distance_column(batch);
        for row in 0..batch.num_rows() {
            hits.push(SemanticHit {
                id: ids.value(row).to_string(),
                kind: kinds.value(row).to_string(),
                text: texts.value(row).to_string(),
                source: sources.value(row).to_string(),
                tags: tags
                    .value(row)
                    .split(',')
                    .map(str::trim)
                    .filter(|tag| !tag.is_empty())
                    .map(str::to_string)
                    .collect(),
                created_at: created.value(row).to_string(),
                distance: distances
                    .as_ref()
                    .and_then(|distances| distances.get(row).copied())
                    .unwrap_or(0.0),
            });
        }
    }
    Ok(hits)
}

pub fn string_column<'a>(batch: &'a RecordBatch, name: &str) -> Result<&'a StringArray> {
    batch
        .column_by_name(name)
        .ok_or_else(|| anyhow!("semantic result missing {name} column"))?
        .as_any()
        .downcast_ref::<StringArray>()
        .ok_or_else(|| anyhow!("semantic result column {name} is not Utf8"))
}

pub fn distance_column(batch: &RecordBatch) -> Option<Vec<f64>> {
    let column = batch.column_by_name("_distance")?;
    if let Some(values) = column.as_any().downcast_ref::<Float32Array>() {
        return Some(
            (0..values.len())
                .map(|index| values.value(index) as f64)
                .collect(),
        );
    }
    if let Some(values) = column.as_any().downcast_ref::<Float64Array>() {
        return Some((0..values.len()).map(|index| values.value(index)).collect());
    }
    None
}

pub fn run_lancedb<T, F>(future: F) -> Result<T>
where
    T: Send + 'static,
    F: Future<Output = Result<T>> + Send + 'static,
{
    thread::spawn(move || {
        tokio::runtime::Builder::new_current_thread()
            .enable_all()
            .build()
            .context("failed to create LanceDB runtime")?
            .block_on(future)
    })
    .join()
    .map_err(|_| anyhow!("LanceDB worker thread panicked"))?
}

fn normalize(vector: &mut [f32]) {
    let norm = vector.iter().map(|value| value * value).sum::<f32>().sqrt();
    if norm > 0.0 {
        for value in vector {
            *value /= norm;
        }
    }
}

fn sanitize_table_name(value: &str) -> String {
    let mut out = value
        .chars()
        .map(|ch| {
            if ch.is_ascii_alphanumeric() || ch == '_' {
                ch
            } else {
                '_'
            }
        })
        .collect::<String>();
    if out.is_empty() {
        out = "memory".to_string();
    }
    out
}

fn env_string(name: &str, default: &str) -> String {
    std::env::var(name)
        .map(|value| value.trim().to_string())
        .ok()
        .filter(|value| !value.is_empty())
        .unwrap_or_else(|| default.to_string())
}

fn env_bool(name: &str, default: bool) -> bool {
    std::env::var(name)
        .map(|value| match value.trim().to_ascii_lowercase().as_str() {
            "1" | "true" | "yes" | "on" => true,
            "0" | "false" | "no" | "off" => false,
            _ => default,
        })
        .unwrap_or(default)
}

#[cfg(test)]
mod tests {
    use tempfile::tempdir;

    use super::*;

    #[test]
    fn hash_embedder_retrieves_semantically_related_documents() {
        let tmp = tempdir().unwrap();
        let index = SemanticIndex::with_hash_embedder(tmp.path(), 64);
        let documents = vec![
            SemanticDocument {
                id: "1".to_string(),
                kind: "note".to_string(),
                text: "Graph API email tokens and Outlook access".to_string(),
                source: "one.md".to_string(),
                tags: vec!["email".to_string()],
                created_at: "2026-05-23".to_string(),
            },
            SemanticDocument {
                id: "2".to_string(),
                kind: "note".to_string(),
                text: "Cargo format and Rust clippy checks".to_string(),
                source: "two.md".to_string(),
                tags: vec!["rust".to_string()],
                created_at: "2026-05-23".to_string(),
            },
        ];

        let results = index
            .search("notes", &documents, "outlook email graph", 1)
            .unwrap();
        assert_eq!(results.len(), 1);
        assert_eq!(results[0].id, "1");
    }
}
