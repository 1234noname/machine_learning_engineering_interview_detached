defmodule Avsa.Orchestrator.V1.StartConversationRequest do
  @moduledoc false

  use Protobuf,
    full_name: "avsa.orchestrator.v1.StartConversationRequest",
    protoc_gen_elixir_version: "0.16.0",
    syntax: :proto3

  field :conversation_id, 1, type: :string, json_name: "conversationId"
  field :image_bytes, 2, repeated: true, type: :bytes, json_name: "imageBytes"
  field :user_text, 3, type: :string, json_name: "userText"
end

defmodule Avsa.Orchestrator.V1.ConversationEvent do
  @moduledoc false

  use Protobuf,
    full_name: "avsa.orchestrator.v1.ConversationEvent",
    protoc_gen_elixir_version: "0.16.0",
    syntax: :proto3

  oneof :payload, 0

  field :product_result, 2,
    type: Avsa.Orchestrator.V1.ProductResultEvent,
    json_name: "productResult",
    oneof: 0
end

defmodule Avsa.Orchestrator.V1.ProductResultEvent do
  @moduledoc false

  use Protobuf,
    full_name: "avsa.orchestrator.v1.ProductResultEvent",
    protoc_gen_elixir_version: "0.16.0",
    syntax: :proto3

  field :product_id, 1, type: :string, json_name: "productId"
  field :score, 2, type: :float
  field :metadata_json, 3, type: :string, json_name: "metadataJson"
end

defmodule Avsa.Orchestrator.V1.Conversation.Service do
  @moduledoc false

  use GRPC.Service, name: "avsa.orchestrator.v1.Conversation", protoc_gen_elixir_version: "0.16.0"

  rpc :StartConversation,
      Avsa.Orchestrator.V1.StartConversationRequest,
      Avsa.Orchestrator.V1.ConversationEvent

  rpc :StreamConversationEvents,
      Avsa.Orchestrator.V1.StartConversationRequest,
      stream(Avsa.Orchestrator.V1.ConversationEvent)
end

defmodule Avsa.Orchestrator.V1.Conversation.Stub do
  @moduledoc false

  use GRPC.Stub, service: Avsa.Orchestrator.V1.Conversation.Service
end
