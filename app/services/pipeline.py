from __future__ import annotations

from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from app.config import PipelineConfig
from app.services.chunk_extractor import (
    ChunkExtractionResult,
    ChunkExtractionService,
    ChunkExtractor,
)
from app.services.chunker import Chunk, SemanticChunker
from app.services.complexity_router import (
    BedrockQwenComplexityClassifier,
    ComplexityRouter,
    PageComplexityRoute,
    VlmComplexityClassifier,
)
from app.services.docling_parser import DoclingMarkdownParser, DoclingParser
from app.services.layout_parser import CheapLayoutParser, RawPdfModel
from app.services.merge_resolve import (
    BedrockQwenSemanticMerger,
    FinalRow,
    MergeResolver,
    SemanticMerger,
)
from app.services.page_preparation import PagePreparer, PreparedPdf
from app.services.parser_path import PageParserArtifact, ParserPath
from app.services.semantic_hints import SemanticHintBuilder, SemanticPageModel
from app.services.vlm_path import (
    BedrockQwenVlmExtractor,
    PageVlmArtifact,
    VlmPageExtractor,
    VlmPath,
)
from app.services.xlsx_writer import write_workbook


class PipelineRunResult(BaseModel):
    model_config = ConfigDict(frozen=True, arbitrary_types_allowed=True)

    pdf_path: Path = Field(description="Path to the source PDF that was processed.")
    prepared_pdf: PreparedPdf = Field(description="Renderer artefacts for the PDF.")
    raw_pdf: RawPdfModel = Field(description="Raw layout model produced by the cheap parser.")
    semantic_pages: list[SemanticPageModel] = Field(description="Semantic page models in document order.")
    complexity_routes: list[PageComplexityRoute] = Field(description="Routing decisions per page.")
    parser_artifacts: list[PageParserArtifact] = Field(description="Parser-path artefacts (parser-routed pages only).")
    vlm_artifacts: list[PageVlmArtifact] = Field(description="VLM-path artefacts (vlm-routed pages only).")
    chunks: list[Chunk] = Field(default_factory=list, description="Chunks produced by the chunker (populated after extract_to_workbook).")
    chunk_results: list[ChunkExtractionResult] = Field(default_factory=list, description="Per-chunk extraction results.")
    final_rows: list[FinalRow] = Field(default_factory=list, description="Final rows after merge/resolve.")

    def to_dict(self) -> dict[str, Any]:
        return {
            "pdf_path": str(self.pdf_path),
            "prepared_pdf": self.prepared_pdf.to_dict(),
            "raw_pdf": self.raw_pdf.to_dict(),
            "semantic_pages": [page.to_dict() for page in self.semantic_pages],
            "complexity_routes": [route.to_dict() for route in self.complexity_routes],
            "parser_artifacts": [artifact.to_dict() for artifact in self.parser_artifacts],
            "vlm_artifacts": [artifact.to_dict() for artifact in self.vlm_artifacts],
            "chunks": [chunk.to_dict() for chunk in self.chunks],
            "chunk_results": [result.to_dict() for result in self.chunk_results],
            "final_rows": [row.to_workbook_dict() for row in self.final_rows],
        }

    def to_semantic_prompt_text(self) -> str:
        page_texts = []
        for page in self.semantic_pages:
            page_texts.append(f"# Page {page.page_number}\n\n{page.to_prompt_text()}")
        return "\n\n".join(page_texts)

    def to_parser_markdown(self) -> str:
        return "\n\n".join(artifact.markdown for artifact in self.parser_artifacts)

    def to_vlm_markdown(self) -> str:
        return "\n\n".join(artifact.markdown for artifact in self.vlm_artifacts)


class ExtractionOutcome(BaseModel):
    model_config = ConfigDict(frozen=True, arbitrary_types_allowed=True)

    pdf_path: Path = Field(description="Path to the source PDF.")
    xlsx_path: Path = Field(description="Path to the produced XLSX workbook.")
    final_rows: list[FinalRow] = Field(description="Final rows written to the workbook.")
    run_result: PipelineRunResult = Field(description="Full pipeline run result, including artefacts and chunk results.")


class CurrentExtractionPipeline:
    def __init__(
        self,
        config: PipelineConfig,
        *,
        layout_parser: CheapLayoutParser | None = None,
        semantic_builder: SemanticHintBuilder | None = None,
        complexity_router: ComplexityRouter | None = None,
        vlm_classifier: VlmComplexityClassifier | None = None,
        parser_path: ParserPath | None = None,
        docling_parser: DoclingParser | None = None,
        vlm_extractor: VlmPageExtractor | None = None,
        vlm_path: VlmPath | None = None,
        chunker: SemanticChunker | None = None,
        chunk_extractor: ChunkExtractor | None = None,
        chunk_extraction_service: ChunkExtractionService | None = None,
        merge_resolver: MergeResolver | None = None,
        semantic_merger: SemanticMerger | None = None,
    ) -> None:
        self.config = config
        self.page_preparer = PagePreparer(config)
        self.layout_parser = layout_parser or CheapLayoutParser()
        self.semantic_builder = semantic_builder or SemanticHintBuilder()
        classifier = vlm_classifier
        if classifier is None and config.bedrock_region:
            classifier = BedrockQwenComplexityClassifier()
        self.complexity_router = complexity_router or ComplexityRouter(
            config,
            vlm_classifier=classifier,
        )
        if parser_path is not None:
            self.parser_path = parser_path
        else:
            self.parser_path = ParserPath(
                docling_parser=docling_parser or DoclingMarkdownParser(),
            )
        extractor = vlm_extractor
        if extractor is None and vlm_path is None and config.bedrock_region:
            extractor = BedrockQwenVlmExtractor()
        self.vlm_path = vlm_path or VlmPath(config, extractor=extractor)
        self.chunker = chunker or SemanticChunker(config)
        if chunk_extraction_service is not None:
            self.chunk_extraction_service = chunk_extraction_service
        else:
            self.chunk_extraction_service = ChunkExtractionService(
                config,
                extractor=chunk_extractor,
            )
        if merge_resolver is not None:
            self.merge_resolver = merge_resolver
        else:
            merger = semantic_merger
            if merger is None and config.bedrock_region is not None:
                merger = BedrockQwenSemanticMerger(config=config)
            self.merge_resolver = MergeResolver(config, semantic_merger=merger)

    def run(self, pdf_path: Path | str) -> PipelineRunResult:
        source = Path(pdf_path)
        prepared_pdf = self.page_preparer.prepare(source)
        raw_pdf = self.layout_parser.parse(source)
        semantic_pages = [
            self.semantic_builder.build_page(page) for page in raw_pdf.pages
        ]
        complexity_routes = self.complexity_router.route_pages(
            raw_pdf.pages,
            prepared_pdf.pages,
        )
        parser_artifacts = self.parser_path.build_pages(
            raw_pdf,
            semantic_pages,
            complexity_routes,
        )
        vlm_artifacts = self.vlm_path.build_pages(
            raw_pdf,
            prepared_pdf,
            complexity_routes,
        )
        if vlm_artifacts:
            vlm_pages = {artifact.page_number: artifact.semantic_page for artifact in vlm_artifacts}
            semantic_pages = [vlm_pages.get(page.page_number, page) for page in semantic_pages]

        return PipelineRunResult(
            pdf_path=source,
            prepared_pdf=prepared_pdf,
            raw_pdf=raw_pdf,
            semantic_pages=semantic_pages,
            complexity_routes=complexity_routes,
            parser_artifacts=parser_artifacts,
            vlm_artifacts=vlm_artifacts,
        )

    def extract_to_workbook(
        self,
        pdf_path: Path | str,
        xlsx_path: Path | str,
    ) -> ExtractionOutcome:
        run_result = self.run(pdf_path)
        chunks = self.chunker.build(
            run_result.parser_artifacts,
            run_result.vlm_artifacts,
            semantic_pages=run_result.semantic_pages,
        )
        chunk_results = (
            self.chunk_extraction_service.extract_all(chunks) if chunks else []
        )
        final_rows = self.merge_resolver.resolve(chunks, chunk_results)

        output_path = Path(xlsx_path)
        write_workbook(final_rows, output_path)

        run_result_with_artifacts = PipelineRunResult(
            pdf_path=run_result.pdf_path,
            prepared_pdf=run_result.prepared_pdf,
            raw_pdf=run_result.raw_pdf,
            semantic_pages=run_result.semantic_pages,
            complexity_routes=run_result.complexity_routes,
            parser_artifacts=run_result.parser_artifacts,
            vlm_artifacts=run_result.vlm_artifacts,
            chunks=chunks,
            chunk_results=chunk_results,
            final_rows=final_rows,
        )

        return ExtractionOutcome(
            pdf_path=Path(pdf_path),
            xlsx_path=output_path,
            final_rows=final_rows,
            run_result=run_result_with_artifacts,
        )
