//! M1n: authenticated, bounded, read-only multipart TXT import preview.
//! Preview never creates a staging row or modifies a book.
use super::{authenticated_read, private_response, ApiError, AppState};
use axum::extract::{Multipart, State};
use axum::http::{HeaderMap, StatusCode};
use axum::response::Response;
use erida_library::import_preview::{self, PreviewError};

fn parse_error(error: PreviewError) -> ApiError {
    match error {
        PreviewError::Invalid(message) => ApiError::InvalidInput(message),
        PreviewError::TooLarge => ApiError::ImportTooLarge,
    }
}

pub async fn preview(
    State(state): State<AppState>,
    headers: HeaderMap,
    mut multipart: Multipart,
) -> Result<Response, ApiError> {
    // Authorize before parsing the uploaded file. On rejected auth, consume
    // the strictly capped body in streaming chunks (without allocating a
    // file buffer). Otherwise HTTP/1.1 uploaders can receive an abrupt
    // connection reset instead of the 401 response when the peer is still
    // writing multipart bytes.
    if let Err(auth_error) = authenticated_read(state, headers, |_, _, _| Ok(())).await {
        while let Ok(Some(mut ignored)) = multipart.next_field().await {
            while matches!(ignored.chunk().await, Ok(Some(_))) {}
        }
        return Err(auth_error);
    }

    let field = multipart.next_field().await
        .map_err(|error| if error.status() == StatusCode::PAYLOAD_TOO_LARGE {
            ApiError::ImportTooLarge
        } else {
            ApiError::InvalidInput("Invalid import multipart data")
        })?
        .ok_or(ApiError::InvalidInput("Missing import file"))?;
    if field.name() != Some("file") {
        return Err(ApiError::InvalidInput("Expected one multipart file field"));
    }
    let filename = field.file_name().ok_or(
        ApiError::InvalidInput("Missing imported file name")
    )?.to_owned();
    let bytes = field.bytes().await
        .map_err(|error| if error.status() == StatusCode::PAYLOAD_TOO_LARGE {
            ApiError::ImportTooLarge
        } else {
            ApiError::InvalidInput("Invalid import multipart data")
        })?;
    if multipart.next_field().await
        .map_err(|error| if error.status() == StatusCode::PAYLOAD_TOO_LARGE {
            ApiError::ImportTooLarge
        } else {
            ApiError::InvalidInput("Invalid import multipart data")
        })?
        .is_some()
    {
        return Err(ApiError::InvalidInput("Expected one multipart file field"));
    }
    let report = import_preview::preview_txt(&filename, &bytes)
        .map_err(parse_error)?;
    Ok(private_response(report))
}
