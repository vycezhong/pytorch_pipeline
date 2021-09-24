import torch

_GLOBAL_ARGS = None
logging_buffer = []
logged_size_in_bytes = 0
memory_budget = 1 * 1000 * 1000 * 1000
logging_stream = torch.cuda.Stream()


def initialize_global_args(args):
    global _GLOBAL_ARGS
    _GLOBAL_ARGS = args


def is_pipeline_last_stage():
    return get_pipeline_model_parallel_rank() == \
        get_pipeline_model_parallel_world_size() - 1


def is_pipeline_first_stage():
    return get_pipeline_model_parallel_rank() == 0


def get_pipeline_model_parallel_world_size():
    return torch.distributed.get_world_size()


def get_pipeline_model_parallel_rank():
    return torch.distributed.get_rank()


def get_pipeline_model_parallel_next_rank():
    return (get_pipeline_model_parallel_rank() + 1) % \
        get_pipeline_model_parallel_world_size()


def get_pipeline_model_parallel_prev_rank():
    return (get_pipeline_model_parallel_rank() - 1) % \
        get_pipeline_model_parallel_world_size()


def get_num_microbatches():
    global _GLOBAL_ARGS
    return _GLOBAL_ARGS.global_batch_size // _GLOBAL_ARGS.micro_batch_size


def get_microbatch_size():
    global _GLOBAL_ARGS
    return _GLOBAL_ARGS.micro_batch_size


def should_logging(dst_rank):
    global _GLOBAL_ARGS
    if not _GLOBAL_ARGS.logging:
        return False
    src_rank = get_pipeline_model_parallel_rank()
    src_node = src_rank // _GLOBAL_ARGS.local_world_size
    dst_node = dst_rank // _GLOBAL_ARGS.local_world_size
    if src_node == dst_node:
        return False
    return True


def forward_step(data_iterator, model, input_tensor, loss_func, loss):
    if is_pipeline_first_stage() or is_pipeline_last_stage():
        data = next(data_iterator)
        images, labels = data
        images, labels = images.cuda(), labels.cuda()

    if is_pipeline_first_stage():
        assert input_tensor is None
        input_tensor = images

    output_tensor = model(input_tensor)

    if is_pipeline_last_stage():
        output_tensor = loss_func(output_tensor, labels)
        output_tensor /= get_num_microbatches()
        loss += output_tensor.item()

    return output_tensor


def backward_step(input_tensor, output_tensor, output_tensor_grad):
    if input_tensor is not None:
        input_tensor.retain_grad()

    torch.autograd.backward(output_tensor, grad_tensors=output_tensor_grad)

    input_tensor_grad = None
    if input_tensor is not None:
        input_tensor_grad = input_tensor.grad

    return input_tensor_grad


def logging():
    global logging_buffer
    global logged_size_in_bytes
    global memory_budget

    for tensor in logging_buffer:
        tensor_cpu = torch.empty_like(tensor, device="cpu", pin_memory=True)
        logging_stream.wait_stream(torch.cuda.current_stream())
        with torch.cuda.stream(logging_stream):
            tensor_cpu.copy_(tensor, non_blocking=True)

        logging_buffer.append(tensor_cpu)
        logged_size_in_bytes += tensor_cpu.numel() * 4
        if logged_size_in_bytes > memory_budget:
            logging_buffer.clear()
            logged_size_in_bytes = 0

    logging_buffer.clear()


def add_to_logging_buffer(tensor):
    global logging_buffer
    logging_buffer.append(tensor)


def send_forward(output_tensor):
    if not is_pipeline_last_stage():
        if should_logging(get_pipeline_model_parallel_next_rank()):
            add_to_logging_buffer(output_tensor)

        torch.distributed.send(
            output_tensor, get_pipeline_model_parallel_next_rank())


def send_backward(input_tensor_grad):
    if not is_pipeline_first_stage():
        if should_logging(get_pipeline_model_parallel_prev_rank()):
            add_to_logging_buffer(input_tensor_grad)

        torch.distributed.send(
            input_tensor_grad, get_pipeline_model_parallel_prev_rank())


def recv_forward(shape, dtype=torch.float32):
    input_tensor = None
    if not is_pipeline_first_stage():
        input_tensor = torch.empty(
            shape, requires_grad=True, device=torch.cuda.current_device(), dtype=dtype)
        torch.distributed.recv(
            input_tensor, get_pipeline_model_parallel_prev_rank())
        return input_tensor


def recv_backward(shape, dtype=torch.float32):
    global logging_buffer
    output_tensor_grad = None
    if not is_pipeline_last_stage():
        if logging_buffer:
            logging()

        output_tensor_grad = torch.empty(
            shape, requires_grad=True, device=torch.cuda.current_device(), dtype=dtype)
        torch.distributed.recv(
            output_tensor_grad, get_pipeline_model_parallel_next_rank())
        return output_tensor_grad


def send_forward_recv_backward(output_tensor, dtype=torch.float32):
    output_tensor_grad = None
    if not is_pipeline_last_stage():
        if should_logging(get_pipeline_model_parallel_next_rank()):
            add_to_logging_buffer(output_tensor)

        output_tensor_grad = torch.empty_like(
            output_tensor, requires_grad=True, device=torch.cuda.current_device(), dtype=dtype)
        send_op = torch.distributed.P2POp(
            torch.distributed.isend, output_tensor, get_pipeline_model_parallel_next_rank())
        recv_op = torch.distributed.P2POp(
            torch.distributed.irecv, output_tensor_grad, get_pipeline_model_parallel_next_rank())
        reqs = torch.distributed.batch_isend_irecv([send_op, recv_op])
        for req in reqs:
            req.wait()

        torch.cuda.synchronize()

    return output_tensor_grad


def send_backward_recv_forward(input_tensor_grad, dtype=torch.float32):
    input_tensor = None
    if not is_pipeline_first_stage():
        if should_logging(get_pipeline_model_parallel_prev_rank()):
            add_to_logging_buffer(input_tensor_grad)

        input_tensor = torch.empty_like(
            input_tensor_grad, requires_grad=True, device=torch.cuda.current_device(), dtype=dtype)
        send_op = torch.distributed.P2POp(
            torch.distributed.isend, input_tensor_grad, get_pipeline_model_parallel_prev_rank())
        recv_op = torch.distributed.P2POp(
            torch.distributed.irecv, input_tensor, get_pipeline_model_parallel_prev_rank())
        reqs = torch.distributed.batch_isend_irecv([send_op, recv_op])
        for req in reqs:
            req.wait()

        torch.cuda.synchronize()

    return input_tensor


def pipedream_flush_schedule(data_iterator, model, loss_func):
    global logging_buffer

    num_microbatches = get_num_microbatches()
    num_warmup_microbatches = get_pipeline_model_parallel_world_size() - \
        get_pipeline_model_parallel_rank() - 1
    num_microbatches_remaining = \
        num_microbatches - num_warmup_microbatches

    input_tensors = []
    output_tensors = []
    loss = torch.tensor(0.0)

    # run warmup forward passes
    for _ in range(num_warmup_microbatches):
        input_tensor = recv_forward(model.input_shape)
        output_tensor = forward_step(
            data_iterator, model, input_tensor, loss_func, loss)
        send_forward(output_tensor)

        input_tensors.append(input_tensor)
        output_tensors.append(output_tensor)

    if num_microbatches > 0:
        input_tensor = recv_forward(model.input_shape)

    # run 1F1B steady state
    for i in range(num_microbatches_remaining):
        first_iteration = (i == 0)
        last_iteration = (i == (num_microbatches_remaining - 1))
        output_tensor = forward_step(
            data_iterator, model, input_tensor, loss_func, loss)

        if first_iteration:
            send_forward(output_tensor)
            output_tensor_grad = recv_backward()
        else:
            output_tensor_grad = send_forward_recv_backward(output_tensor)

        input_tensors.append(input_tensor)
        output_tensors.append(output_tensor)

        input_tensor = input_tensors.pop(0)
        output_tensor = output_tensors.pop(0)

        input_tensor_grad = backward_step(
            input_tensor, output_tensor, output_tensor_grad)

        if last_iteration:
            send_backward(input_tensor_grad)
        else:
            input_tensor = send_backward_recv_forward(input_tensor_grad)

    # run cooldown backward pass
    for i in range(num_warmup_microbatches):
        input_tensor = input_tensors.pop(0)
        output_tensor = output_tensors.pop(0)

        output_tensor_grad = recv_backward(model.output_shape)

        input_tensor_grad = backward_step(
            input_tensor, output_tensor, output_tensor_grad)

        send_backward(input_tensor_grad)

    if logging_buffer:
        logging()

    return loss.item()
