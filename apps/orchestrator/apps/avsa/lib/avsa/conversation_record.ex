defmodule AVSA.ConversationRecord do
  @moduledoc """
  Ecto schema for conversations.conversations and conversations.turns.

  Provides insert helpers called by AVSA.Conversation when the Repo is running.
  All DB writes are best-effort — if the Repo is not started (e.g. in stub tests),
  the inserts are silently skipped so the rest of the flow continues.
  """

  use Ecto.Schema

  @schema_prefix "conversations"
  @primary_key {:id, :binary_id, autogenerate: false}
  @timestamps_opts [type: :utc_datetime_usec, inserted_at: :created_at, updated_at: false]

  schema "conversations" do
    field(:expires_at, :utc_datetime_usec)
    timestamps()
  end

  defmodule Turn do
    @moduledoc false

    use Ecto.Schema

    @schema_prefix "conversations"
    @primary_key {:id, :binary_id, autogenerate: true}
    @timestamps_opts [type: :utc_datetime_usec, inserted_at: :created_at, updated_at: false]

    schema "turns" do
      field(:conversation_id, :binary_id)
      field(:role, :string)
      field(:content, :map)
      timestamps()
    end
  end

  @doc """
  Insert a new conversation record. Silently returns :ok if the Repo is not started.
  """
  @spec insert_conversation(String.t()) :: :ok
  def insert_conversation(conversation_id) do
    with true <- repo_running?(),
         {:ok, uuid} <- Ecto.UUID.cast(conversation_id) do
      expires_at = DateTime.add(DateTime.utc_now(), 24 * 60 * 60, :second)

      case AVSA.Repo.insert(
             %__MODULE__{id: uuid, expires_at: expires_at},
             on_conflict: :nothing
           ) do
        {:ok, _} -> :ok
        {:error, _} -> :ok
      end
    end

    :ok
  end

  @doc """
  Insert a turn record. Silently returns :ok if the Repo is not started.
  """
  @spec insert_turn(String.t(), String.t(), map()) :: :ok
  def insert_turn(conversation_id, role, content) do
    with true <- repo_running?(),
         {:ok, uuid} <- Ecto.UUID.cast(conversation_id) do
      case AVSA.Repo.insert(%Turn{conversation_id: uuid, role: role, content: content}) do
        {:ok, _} -> :ok
        {:error, _} -> :ok
      end
    end

    :ok
  end

  defp repo_running? do
    Application.get_env(:avsa, :start_repo, true) and
      Process.whereis(AVSA.Repo) != nil
  end
end
