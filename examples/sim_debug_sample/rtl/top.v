module top (
    input wire clk,
    input wire rst_n,
    input wire acc_valid,
    input wire [7:0] acc_data,
    output reg out_valid,
    output reg [7:0] out_data
);
    localparam IDLE = 2'b00;
    localparam ACCUM = 2'b01;
    localparam DONE = 2'b10;

    reg [1:0] state;

    always @(posedge clk or negedge rst_n) begin
        if (!rst_n) begin
            state <= IDLE;
            out_valid <= 1'b0;
            out_data <= 8'h00;
        end else begin
            if (acc_valid) begin
                state <= DONE;
                out_valid <= 1'b1;
                out_data <= acc_data - 8'h04;
            end
        end
    end
endmodule
