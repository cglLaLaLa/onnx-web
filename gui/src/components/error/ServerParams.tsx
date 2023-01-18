import { Alert, AlertTitle, Typography } from '@mui/material';
import * as React from 'react';

export interface ServerParamsErrorProps {
  error: unknown;
  root: string;
}

function getErrorMessage(error: unknown): string {
  if (error instanceof Error) {
    return error.message;
  } else if (typeof error === 'string') {
    return error;
  } else {
    return 'unknown error';
  }
}

export function ServerParamsError(props: ServerParamsErrorProps) {
  return <Alert severity='error'>
    <AlertTitle>
      Server Error
    </AlertTitle>
    <Typography>
      Could not fetch parameters from the ONNX web API server at <code>{props.root}</code>.
    </Typography>
    <Typography>
      {getErrorMessage(props.error)}
    </Typography>
  </Alert>;
}
