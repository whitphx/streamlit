/**
 * Copyright (c) Streamlit Inc. (2018-2022) Snowflake Inc. (2022-2024)
 * Copyright (c) Yuichiro Tachibana (Tsuchiya) (2022-2024)
 *
 * Licensed under the Apache License, Version 2.0 (the "License");
 * you may not use this file except in compliance with the License.
 * You may obtain a copy of the License at
 *
 *     http://www.apache.org/licenses/LICENSE-2.0
 *
 * Unless required by applicable law or agreed to in writing, software
 * distributed under the License is distributed on an "AS IS" BASIS,
 * WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
 * See the License for the specific language governing permissions and
 * limitations under the License.
 */

/**
 * Stlite:
 * We customize ConnectionManager to connect to the stlite worker instead of the remote Streamlit server.
 * We are emulating WebSocket connection with minimal features needed for stlite,
 * so we omit `WebsocketConnection` for simplicity which is used in the original `ConnectionManager`
 * since the complex features it provides are not necessary for stlite's simplified WebSocket-like protocol.
 * In this stlite version, `ConnectionManager` directly handles the WebSocket-like communication with the stlite worker.
 */

import { ReactNode } from "react"

import {
  BackMsg,
  BaseUriParts,
  ensureError,
  ForwardMsg,
  IHostConfigResponse,
  logError,
  SessionInfo,
  StreamlitEndpoints,
} from "@streamlit/lib"

import { ConnectionState } from "./ConnectionState"
import {
  type StliteKernel,
  DUMMY_BASE_HOSTNAME,
  DUMMY_BASE_PORT,
} from "@stlite/kernel"

/**
 * When the websocket connection retries this many times, we show a dialog
 * letting the user know we're having problems connecting. This happens
 * after about 15 seconds as, before the 6th retry, we've set timeouts for
 * a total of approximately 0.5 + 1 + 2 + 4 + 8 = 15.5 seconds (+/- some
 * due to jitter).
 */
const RETRY_COUNT_FOR_WARNING = 6

interface Props {
  /**
   * Stlite: a stlite kernel object to connect to.
   */
  kernel: StliteKernel

  /** The app's SessionInfo instance */
  sessionInfo: SessionInfo

  /** The app's StreamlitEndpoints instance */
  endpoints: StreamlitEndpoints

  /**
   * Function to be called when we receive a message from the server.
   */
  onMessage: (message: ForwardMsg) => void

  /**
   * Function to be called when the connection errors out.
   */
  onConnectionError: (errNode: ReactNode) => void

  /**
   * Called when our ConnectionState is changed.
   */
  connectionStateChanged: (connectionState: ConnectionState) => void

  /**
   * Function to get the auth token set by the host of this app (if in a
   * relevant deployment scenario).
   */
  claimHostAuthToken: () => Promise<string | undefined>

  /**
   * Function to clear the withHostCommunication hoc's auth token. This should
   * be called after the promise returned by claimHostAuthToken successfully
   * resolves.
   */
  resetHostAuthToken: () => void

  /**
   * Function to set the host config for this app (if in a relevant deployment
   * scenario).
   */
  onHostConfigResp: (resp: IHostConfigResponse) => void
}

// Stlite: Copied from WebsocketConnection.tsx
interface MessageQueue {
  [index: number]: any
}

/**
 * Manages our connection to the Server.
 */
export class ConnectionManager {
  private readonly props: Props

  private connectionState: ConnectionState = ConnectionState.INITIAL

  constructor(props: Props) {
    this.props = props

    // This method returns a promise, but we don't care about its result.
    this.connect()
  }

  /**
   * Indicates whether we're connected to the server.
   */
  public isConnected(): boolean {
    return this.connectionState === ConnectionState.CONNECTED
  }

  /**
   * Return the BaseUriParts for the server we're connected to,
   * if we are connected to a server.
   */
  public getBaseUriParts(): BaseUriParts | undefined {
    if (this.connectionState === ConnectionState.CONNECTED) {
      // Stlite:
      // This property became necessary for multi-page apps: https://github.com/streamlit/streamlit/pull/4698/files#diff-e56cb91573ddb6a97ecd071925fe26504bb5a65f921dc64c63e534162950e1ebR967-R975
      // so here a dummy BaseUriParts object is returned.
      // The host and port are set as dummy values that are invalid as a URL
      // in order to avoid unexpected accesses to external resources,
      // while the basePath is representing the actual info.
      return {
        host: DUMMY_BASE_HOSTNAME,
        port: DUMMY_BASE_PORT,
        // When a new session starts, a page name for multi-page apps (a relative path to the app root url) is calculated based on this `basePath`
        // then a `rerunScript` BackMsg is sent to the server with `pageName` (https://github.com/streamlit/streamlit/blob/ace58bfa3582d4f8e7f281b4dbd266ddd8a32b54/frontend/src/App.tsx#L1064)
        // and `window.history.pushState` is called (https://github.com/streamlit/streamlit/blob/ace58bfa3582d4f8e7f281b4dbd266ddd8a32b54/frontend/src/App.tsx#L665).
        basePath: this.props.kernel.basePath,
      }
    }
    return undefined
  }

  public sendMessage(obj: BackMsg): void {
    // Stltie: we call `kernel.sendWebSocketMessage` directly here instead of using `WebsocketConnection`.
    if (this.isConnected()) {
      const msg = BackMsg.create(obj)
      const buffer = BackMsg.encode(msg).finish()
      this.props.kernel.sendWebSocketMessage(buffer)
    } else {
      // Don't need to make a big deal out of this. Just print to console.
      logError(`Cannot send message when server is disconnected: ${obj}`)
    }
  }

  /**
   * Increment the runCount on our message cache, and clear entries
   * whose age is greater than the max.
   */
  public incrementMessageCacheRunCount(maxMessageAge: number): void {
    // Stlite: no-op.
    // Caching is disabled in stlite. See https://github.com/whitphx/stlite/issues/495
  }

  private async connect(): Promise<void> {
    // Stlite: we connect to the stlite worker by calling `kernel.connectWebSocket` directly here instead of using `WebsocketConnection`.
    const WEBSOCKET_STREAM_PATH = "_stcore/stream" // The original is defined in streamlit/frontend/src/lib/WebsocketConnection.tsx

    try {
      this.props.kernel.onWebSocketMessage(payload => {
        if (typeof payload === "string") {
          logError("Unexpected payload type.")
          return
        }
        this.handleMessage(payload)
      })

      await this.props.kernel.loaded
      console.debug("The kernel has been loaded. Start connecting.")
      this.props.onHostConfigResp(this.props.kernel.hostConfigResponse)

      await this.props.kernel.connectWebSocket("/" + WEBSOCKET_STREAM_PATH)
      this.setConnectionState(ConnectionState.CONNECTED)
    } catch (e) {
      const err = ensureError(e)
      logError(err.message)
      this.setConnectionState(
        ConnectionState.DISCONNECTED_FOREVER,
        err.message
      )
    }
  }

  disconnect(): void {
    // Stlite: no-op.
    // We don't need to consider disconnection in stlite because it's not a remote connection.
  }

  // Stlite: the following properties and methods are copied from WebsocketConnection.tsx

  /**
   * To guarantee packet transmission order, this is the index of the last
   * dispatched incoming message.
   */
  private lastDispatchedMessageIndex = -1

  /**
   * And this is the index of the next message we receive.
   */
  private nextMessageIndex = 0

  /**
   * This dictionary stores received messages that we haven't sent out yet
   * (because we're still decoding previous messages)
   */
  private readonly messageQueue: MessageQueue = {}

  private async handleMessage(data: ArrayBuffer): Promise<void> {
    // Assign this message an index.
    const messageIndex = this.nextMessageIndex
    this.nextMessageIndex += 1

    const encodedMsg = new Uint8Array(data)
    const msg = ForwardMsg.decode(encodedMsg)

    // Stlite: doesn't handle caches.
    if (msg.type === "refHash") {
      throw new Error(`Unexpected cache reference message.`)
    }

    this.messageQueue[messageIndex] = msg

    // Dispatch any pending messages in the queue. This may *not* result
    // in our just-decoded message being dispatched: if there are other
    // messages that were received earlier than this one but are being
    // downloaded, our message won't be sent until they're done.
    while (this.lastDispatchedMessageIndex + 1 in this.messageQueue) {
      const dispatchMessageIndex = this.lastDispatchedMessageIndex + 1
      this.props.onMessage(this.messageQueue[dispatchMessageIndex])
      delete this.messageQueue[dispatchMessageIndex]
      this.lastDispatchedMessageIndex = dispatchMessageIndex
    }
  }

  private setConnectionState = (
    connectionState: ConnectionState,
    errMsg?: string
  ): void => {
    if (this.connectionState !== connectionState) {
      this.connectionState = connectionState
      this.props.connectionStateChanged(connectionState)
    }

    if (errMsg) {
      this.props.onConnectionError(errMsg)
    }
  }
}
